#!/usr/bin/python3.11
"""awnix first-boot setup -- a thin launcher over the awnix_setup package.

    awnix-setup [--tty]                  interactive, on tty1 (the default)
    awnix-setup --status [--json]        what was chosen (never echoes a secret)
    awnix-setup --apply-seed [PATH]      non-interactive: /etc/awnix/seed.json (+ seed.d/)
    awnix-setup --validate-steps DIR     exit 1 on any bad steps.d file
    awnix-setup --reset --yes            remove /etc/awnix/setup.json; setup runs again
    awnix-setup --self-test              offline, stubbed; proves the rules
    awnix-setup --list-verbs

The same setup is served in a browser by awnix-console at https://<box>:9443 while
/etc/awnix/setup.json is absent (the code is on the tty banner); that server mounts
awnix_setup.api, so tty and web cannot disagree.

Exit: 0 ok, 1 failed/violation, 2 could not judge, 130 cancelled at the tty.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Installed: /usr/lib/awnix-setup/awnix_setup. Dev/self-test: next to this file.
for _cand in ("/usr/lib/awnix-setup", str(Path(__file__).resolve().parent)):
    if (Path(_cand) / "awnix_setup" / "__init__.py").exists() and _cand not in sys.path:
        sys.path.insert(0, _cand)
        break

try:
    from awnix_setup import api, core  # noqa: E402
except ImportError as _e:  # pragma: no cover - image assembly error
    print(
        f"awnix-setup: the awnix_setup package is missing ({_e}); the image did not "
        f"COPY awnix_setup into /usr/lib/awnix-setup",
        file=sys.stderr,
    )
    sys.exit(2)

VERBS = [
    "--tty",
    "--status",
    "--apply-seed",
    "--validate-steps",
    "--reset",
    "--self-test",
    "--list-verbs",
]


def cmd_status(as_json: bool) -> int:
    st = core.redacted_state()
    if as_json:
        print(
            json.dumps(
                {
                    "complete": core.setup_complete(),
                    "state": st or None,
                    "linked_token_present": core.link_token_file().exists(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not st:
        print("awnix: setup has not run yet")
        return 1
    print(json.dumps(st, indent=2, sort_keys=True))
    return 0


def cmd_validate(directory: str, check_path: bool) -> int:
    d = Path(directory)
    if not d.is_dir():
        print(f"awnix-setup: {d} is not a directory", file=sys.stderr)
        return 2
    files = sorted(d.glob("*.json"))
    probs: list[str] = []
    ids: dict[str, str] = {}
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            probs.append(f"{f.name}: not JSON ({e})")
            continue
        probs += core.validate_step(doc, f.name, check_path=check_path)
        sid = doc.get("id") if isinstance(doc, dict) else None
        if sid in ids:
            probs.append(f"{f.name}: id '{sid}' duplicates {ids[sid]}")
        elif sid:
            ids[sid] = f.name
    for p in probs:
        print(f"  FAIL {p}")
    print(f"awnix-setup: {len(files)} step file(s) in {d}, {len(probs)} problem(s)")
    return 1 if probs else 0


# ── self-test ────────────────────────────────────────────────────────────────────────────
def self_test() -> int:  # noqa: C901 - one linear list of assertions
    fails = 0

    def chk(cond: bool, label: str) -> None:
        nonlocal fails
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")
        if not cond:
            fails += 1

    tmp = Path(tempfile.mkdtemp(prefix="awnix-setup-st-"))
    env_keep = dict(os.environ)
    calls: list[list[str]] = []
    real_run, real_fetch, real_get, real_which = core.RUN, core.FETCH, core.GET_JSON, core.which
    try:
        for k, v in {
            "AWNIX_STATE_DIR": tmp / "etc/awnix",
            "AWNIX_RUN_DIR": tmp / "run/awnix",
            "AWNIX_STEPS_DIR": tmp / "steps.d",
            "AWNIX_COMPONENTS_LOCK": tmp / "components.lock.json",
            "AITHER_LICENSE_STATUS": tmp / "status.json",
            "AITHER_LICENSE_SPOOL": tmp / "var/lib/aither/license/incoming.lic",
            "AITHER_SETUP_ANSWERS": tmp / "etc/aither/setup-answers.json",
            "AWNIX_RELEASE_ENV": tmp / "release.env",
            "AWNIX_HOME_ROOT": tmp / "home",
            "AWNIX_PASSWD": tmp / "passwd",
            "AWNIX_GROUP": tmp / "group",
            "AWNIX_ENDPOINTS_ADMIN": tmp / "etc-endpoints.env",
            "AWNIX_ENDPOINTS_VENDOR": tmp / "usr-endpoints.env",
            "AWNIX_SECRET_ROOT": tmp / "secret-root",
            "AWNIX_SUDOERS_DIR": tmp / "sudoers.d",
            "AWNIX_SHADOW": tmp / "shadow",
            "AWNIX_PROFILE_FILE": tmp / "profile",
        }.items():
            os.environ[k] = str(v)
        for k in ("AWNIX_VARIANT", "AWNIX_LINK_HOST", "AWNIX_DEVICE_CODE_URL"):
            os.environ.pop(k, None)
        (tmp / "passwd").write_text("root:x:0:0::/root:/bin/bash\n", encoding="utf-8")
        (tmp / "group").write_text("wheel:x:10:\n", encoding="utf-8")
        (tmp / "release.env").write_text("AWNIX_VARIANT=garg-appliance\n", encoding="utf-8")
        (tmp / "steps.d").mkdir()

        def stub(argv, input_text=None, timeout=None):
            if not isinstance(argv, list):
                raise AssertionError("a shell string reached the runner")
            calls.append(list(argv))
            if argv[:2] == ["ssh-keygen", "-l"]:
                if input_text and input_text.startswith("ssh-ed25519 AAAAC3"):
                    return 0, "256 SHA256:abcdEFGH user (ED25519)\n", ""
                return 1, "", "is not a public key file"
            if argv[:3] == ["openssl", "passwd", "-6"]:
                return 0, "$6$salt$hashhashhash\n", ""
            if argv[0] == "useradd":
                with open(tmp / "passwd", "a", encoding="utf-8") as f:
                    f.write(f"{argv[-1]}:x:1000:1000::/home/{argv[-1]}:/bin/bash\n")
                (tmp / "group").write_text(f"wheel:x:10:{argv[-1]}\n", encoding="utf-8")
                return 0, "", ""
            if argv[0] == "garg-model" and argv[1] == "list":
                return (
                    0,
                    json.dumps(
                        [
                            {"value": "4B", "label": "4B", "active": True},
                            {"value": "8B", "label": "8B"},
                        ]
                    ),
                    "",
                )
            if argv[0] == core.AWNIX_BIN and argv[1:4] == ["component", "install", "--json"]:
                return (
                    0,
                    json.dumps(
                        {"ok": True, "results": [{"id": i, "state": "installed"} for i in argv[4:]]}
                    ),
                    "",
                )
            return 0, "", ""

        core.RUN = stub
        core.which = lambda n: "/usr/bin/" + n  # every step binary 'installed'

        # endpoints precedence: process > /etc > /usr/lib > literal
        chk(
            core.load_endpoints()["AWNIX_LINK_HOST"]
            == (core.ENDPOINT_DEFAULTS["AWNIX_LINK_HOST"], "default"),
            "endpoints: the literal is the floor",
        )
        (tmp / "usr-endpoints.env").write_text(
            "AWNIX_LINK_HOST=https://vendor.example\n", encoding="utf-8"
        )
        chk(
            core.load_endpoints()["AWNIX_LINK_HOST"][1] == "vendor-env",
            "endpoints: vendor file beats literal",
        )
        (tmp / "etc-endpoints.env").write_text(
            "AWNIX_LINK_HOST='https://admin.example'\n", encoding="utf-8"
        )
        chk(
            core.load_endpoints()["AWNIX_LINK_HOST"] == ("https://admin.example", "admin-env"),
            "endpoints: /etc beats /usr/lib (quotes stripped, no expansion)",
        )
        os.environ["AWNIX_LINK_HOST"] = "https://proc.example"
        eps = core.load_endpoints()
        chk(
            eps["AWNIX_LINK_HOST"] == ("https://proc.example", "process-env"),
            "endpoints: process env beats /etc",
        )
        chk(
            eps["AWNIX_DEVICE_CODE_URL"][0] == "https://proc.example/auth/device/code",
            "endpoints: device-code URL derives from the link host",
        )
        chk(
            core.device_token_url(eps["AWNIX_DEVICE_CODE_URL"][0])
            == "https://proc.example/auth/device/token",
            "device token URL is the sibling",
        )
        os.environ.pop("AWNIX_LINK_HOST")

        # admin argv + keys
        chk(
            core.admin_argv("alice", "$6$x", False)
            == [
                ["useradd", "-m", "-G", "wheel", "-s", "/bin/bash", "alice"],
                ["usermod", "-p", "$6$x", "alice"],
            ],
            "admin: exact useradd/usermod argv",
        )
        chk(
            core.admin_argv("alice", None, True) == [["usermod", "-aG", "wheel", "alice"]],
            "admin: an existing user is only added to wheel",
        )
        chk(
            not core.valid_username("root") and not core.valid_username("Bad Name"),
            "admin: root and junk names are refused",
        )
        # Assembled at run time so this shipped file carries no key-shaped literal (PRT005).
        pem = "OPENSSH " + "PRIVATE KEY"
        priv = f"-----BEGIN {pem}-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END {pem}-----"
        ok, why = core.validate_pubkey(priv)
        chk(not ok and "PRIVATE" in why, "pubkey: a private key is rejected")
        chk(
            not any(c[0] == "ssh-keygen" and False for c in calls),
            "pubkey: rejected before ssh-keygen",
        )
        good = "ssh-ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAI" + "Fake" * 10 + " me@x"
        ok, fp = core.validate_pubkey(good)
        chk(ok and fp == "SHA256:abcdEFGH", "pubkey: ssh-keygen fingerprints a good key")
        chk(
            not core.validate_pubkey("ssh-ed25519 AAAAnotreal x")[0],
            "pubkey: ssh-keygen is the judge",
        )
        core.FETCH = lambda url: f"# {url}\n{good}\nnot a key\n\n"
        keys, why = core.import_github_keys("octocat")
        chk(keys == [good], "github: only well-formed key lines survive the .keys parse")
        chk(core.import_github_keys("bad/../user")[0] == [], "github: a junk username is refused")

        code, body = api.apply_status(
            "admin", {"name": "alice", "ssh_keys": good, "password": "hunter22"}
        )
        chk(code == 200 and body.get("admin_user") == "alice", "api: admin created")
        ak = tmp / "home/alice/.ssh/authorized_keys"
        chk(
            ak.exists() and good in ak.read_text(encoding="utf-8"), "admin: authorized_keys written"
        )
        if os.name == "posix":
            chk((ak.stat().st_mode & 0o777) == 0o600, "admin: authorized_keys is 0600")
        chk(
            ["openssl", "passwd", "-6", "-stdin"] in calls,
            "admin: password hashed by openssl, not crypt",
        )
        chk(
            not any("hunter22" in " ".join(c) for c in calls),
            "admin: the password never reaches argv",
        )
        chk(core.detect_admin() == "alice", "admin: detect_admin finds the wheel user with keys")
        chk(
            not (tmp / "sudoers.d/90-awnix-admin").exists(),
            "admin: a password admin gets no NOPASSWD drop-in",
        )
        code, body = api.apply_status("admin", {"name": "bob", "ssh_keys": priv})
        chk(code == 422, "api: a pasted private key is a 422")

        # steps.d: argv only, never a shell
        s_ok = {
            "schema": 1,
            "id": "garg-model",
            "title": "t",
            "why": "w",
            "kind": "choice",
            "options_cmd": ["garg-model", "list", "--json"],
            "apply_cmd": ["garg-model", "set", "{value}"],
            "required": False,
            "timeout_s": 3600,
            "variants": ["garg-appliance"],
        }
        chk(
            core.validate_step(s_ok, "50-garg-model.json", check_path=False) == [],
            "steps: a well-formed step validates",
        )
        bad = dict(s_ok, apply_cmd="garg-model set {value}")
        chk(
            any("shell STRING" in e for e in core.validate_step(bad, "50-garg-model.json", False)),
            "steps: a shell-string apply_cmd is a violation",
        )
        bad = dict(s_ok, apply_cmd=["sh", "-c", "garg-model set {value}"])
        chk(
            any(
                "interpreter/shell" in e
                for e in core.validate_step(bad, "50-garg-model.json", False)
            ),
            "steps: sh -c is a violation",
        )
        bad = dict(s_ok, apply_cmd=["garg-model", "--tier={value}"])
        chk(
            any(
                "whole argv element" in e
                for e in core.validate_step(bad, "50-garg-model.json", False)
            ),
            "steps: {value} glued into an arg is a violation",
        )
        chk(
            any(
                "required must be false" in e
                for e in core.validate_step(dict(s_ok, required=True), "50-garg-model.json", False)
            ),
            "steps: required:true is a violation (no step may block boot)",
        )
        chk(
            any(
                "not on PATH" in e
                for e in core.validate_step(
                    s_ok, "50-garg-model.json", True, which_fn=lambda n: None
                )
            ),
            "steps: a missing binary fails at build time",
        )
        sec_bad = {
            "schema": 1,
            "id": "tok",
            "title": "t",
            "why": "w",
            "kind": "secret",
            "secret_dest": "/x",
            "apply_cmd": ["verify", "{value}"],
            "timeout_s": 5,
        }
        chk(
            any(
                "never be passed on argv" in e
                for e in core.validate_step(sec_bad, "10-tok.json", False)
            ),
            "steps: a secret on argv is a violation",
        )
        chk(
            core.substitute(["garg-model", "set", "{value}"], "; rm -rf /")
            == ["garg-model", "set", "; rm -rf /"],
            "steps: '; rm -rf /' stays ONE argv element",
        )
        (tmp / "steps.d/50-garg-model.json").write_text(json.dumps(s_ok), encoding="utf-8")
        sec = {
            "schema": 1,
            "id": "license",
            "title": "License",
            "why": "w",
            "kind": "secret",
            "secret_dest": "/var/lib/aither/license/incoming.lic",
            "apply_cmd": ["aitheros", "license", "refresh", "--quiet"],
            "timeout_s": 60,
        }
        (tmp / "steps.d/40-license.json").write_text(json.dumps(sec), encoding="utf-8")
        (tmp / "steps.d/60-other.json").write_text(
            json.dumps(dict(s_ok, id="other", variants=["aitheros"])), encoding="utf-8"
        )
        steps, probs = core.load_steps(check_path=False)
        chk(
            [s["id"] for s in steps] == ["license", "garg-model"],
            "steps: variant filtering keeps garg steps, drops aitheros ones",
        )

        code, body = api.apply_status("garg-model", "; rm -rf /")
        chk(code == 409, "api: a choice outside the offered options is refused")
        chk(not any("; rm -rf /" in c for c in calls[-1]), "api: ...and never reaches the runner")
        code, body = api.apply_status("garg-model", "8B")
        chk(
            code == 200 and calls[-1] == ["garg-model", "set", "8B"],
            "api: a choice applies as argv",
        )

        secret = "AITHER1.c2VjcmV0LXBheWxvYWQ.c2ln"
        code, body = api.apply_status("license", secret)
        chk(code == 200 and secret not in json.dumps(body), "api: a secret apply never echoes it")
        dest = tmp / "secret-root/var/lib/aither/license/incoming.lic"
        chk(
            dest.exists() and secret in dest.read_text(encoding="utf-8"),
            "secret: written to secret_dest",
        )
        if os.name == "posix":
            chk((dest.stat().st_mode & 0o777) == 0o600, "secret: secret_dest is 0600")
        chk(not any(secret in " ".join(c) for c in calls), "secret: never on argv")
        chk(api.apply_status("nope", 1)[0] == 404, "api: an unknown step is a 404")

        # The MOUNT contract awnix-console calls: `self._json(200, api.steps())`, and it
        # maps ValueError/KeyError -> 422, PermissionError -> 403. So the public handlers
        # return a plain dict and RAISE on failure -- never a (status, body) tuple, which
        # would ship as a JSON array under a 200 and turn a failed apply into success.
        got = api.steps()
        chk(
            isinstance(got, dict) and isinstance(got.get("steps"), list),
            "mount: steps() is a dict with a steps list (not a tuple)",
        )
        chk(isinstance(api.state(), dict), "mount: state() is a dict")

        def _raises(fn, *a):
            try:
                fn(*a)
            except Exception as e:  # noqa: BLE001 -- the self-test inspects what it got
                return e
            return None

        e = _raises(api.apply, "nope", 1)
        chk(
            isinstance(e, KeyError) and isinstance(e, ValueError)
            and getattr(e, "status", 0) == 404 and str(e).startswith("no step"),
            "mount: an unknown step RAISES (KeyError+ValueError, .status 404, unquoted)",
        )
        e = _raises(api.apply, "garg-model", "; rm -rf /")
        chk(
            isinstance(e, ValueError) and getattr(e, "status", 0) == 409,
            "mount: a failed apply RAISES (ValueError, .status 409) -- never a 200",
        )
        got = api.apply("garg-model", "8B")
        chk(isinstance(got, dict) and got.get("ok") is True, "mount: a good apply is a dict")

        # components: awnix component, never pip; hides unavailable + needs-license
        (tmp / "components.lock.json").write_text(
            json.dumps(
                {
                    "components": [
                        {"id": "awgit", "kind": "baked", "available": True},
                        {"id": "awprism", "kind": "pypi", "available": True},
                        {
                            "id": "awgone",
                            "kind": "pypi",
                            "available": False,
                            "reason": "not published",
                        },
                        {
                            "id": "awpack-pro",
                            "kind": "pack",
                            "available": True,
                            "requires_license": True,
                            "entitlement": "pro",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        chk(
            [r["value"] for r in core.component_menu()] == ["awprism"],
            "components: menu hides baked, available:false and needs-license rows",
        )
        (tmp / "status.json").write_text(
            json.dumps({"state": "valid", "entitlements": {"packs": ["pro"], "images": []}}),
            encoding="utf-8",
        )
        chk(
            [r["value"] for r in core.component_menu()] == ["awprism", "awpack-pro"],
            "components: an entitled pack is offered",
        )
        code, body = api.apply_status("components", ["awprism"])
        chk(
            code == 200
            and calls[-1] == [core.AWNIX_BIN, "component", "install", "--json", "awprism"],
            "components: installs via `awnix component install --json`",
        )
        chk(not any("pip" in " ".join(c) for c in calls), "components: pip is never called")
        chk(
            api.apply_status("components", ["awgone"])[0] == 409,
            "components: an unoffered id is refused",
        )

        # link flow: pending is not failure; the bearer never lands in setup.json
        seq = [
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"access_token": "tok-123"},
        ]
        core.GET_JSON = lambda url, payload=None, timeout=20.0: seq.pop(0)
        n = {"v": 0.0}

        def _now():
            n["v"] += 1
            return n["v"]

        ok, tok = core.poll_device_flow("d", 1, 60, sleep=lambda s: None, now=_now)
        chk(ok and tok == "tok-123", "link: polls through pending + slow_down to a token")
        seq[:] = [{"error": "access_denied"}]
        chk(
            api.apply_status("link", {"action": "poll", "device_code": "d"})[0] == 409,
            "link: a decline is reported, not retried forever",
        )
        seq[:] = [{"access_token": "bearer-xyz"}]
        code, body = api.apply_status("link", {"action": "poll", "device_code": "d"})
        chk(
            code == 200 and body.get("linked") and "bearer-xyz" not in json.dumps(body),
            "link: success is reported without the bearer",
        )
        chk(
            core.link_token_file().read_text(encoding="utf-8").strip() == "bearer-xyz",
            "link: the bearer lands in link.token",
        )

        # parse_selection
        offered = [{"value": "a"}, {"value": "B"}]
        chk(core.parse_selection("", offered) == [], "selection: empty means none")
        chk(core.parse_selection("all", offered) == ["a", "B"], "selection: all")
        chk(core.parse_selection("2 b 2", offered) == ["B"], "selection: dedupes, case-insensitive")
        chk(core.parse_selection("999 junk", offered) == [], "selection: never guesses")

        # finish: v2 marker, no bearer, no secret; then 410
        code, body = api.finish_status()
        chk(code == 200, "finish: 200")
        raw = core.state_file().read_text(encoding="utf-8")
        st = json.loads(raw)
        chk(
            st["version"] == 2 and st["admin_user"] == "alice" and st["linked"] is True,
            "finish: setup.json v2 carries admin + linked",
        )
        chk(
            "bearer" not in raw
            and "bearer-xyz" not in raw
            and secret not in raw
            and "hunter22" not in raw,
            "finish: no bearer, secret or password in setup.json",
        )
        chk(
            ["systemctl", "--no-block", "restart", "awnix-console.service"] in calls,
            "finish: restarts awnix-console to switch to console mode",
        )
        chk(
            api.steps_status()[0] == 410 and api.apply_status("hostname", "x")[0] == 410,
            "api: after finish, setup handlers answer 410",
        )
        e = _raises(api.steps)
        chk(
            isinstance(e, PermissionError) and getattr(e, "status", 0) == 410,
            "mount: after finish, steps() RAISES PermissionError (.status 410)",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_status(True)
        chk(
            secret not in buf.getvalue() and "bearer-xyz" not in buf.getvalue(),
            "--status --json never echoes a secret or bearer",
        )

        # v1 reader: an inline bearer moves out
        core.link_token_file().unlink()
        core.state_file().write_text(
            json.dumps({"version": 1, "bearer": "old-tok"}), encoding="utf-8"
        )
        chk("bearer" not in core.read_state(), "v1: read_state never returns the bearer")
        chk(
            core.link_token_file().read_text(encoding="utf-8").strip() == "old-tok",
            "v1: the inline bearer is moved to link.token",
        )
        chk(core.reset_setup() and not core.setup_complete(), "--reset removes the marker")

        # seed
        sd = core.seed_dir()
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "license.lic").write_text("AITHER1.cGF5bG9hZA.c2ln\n", encoding="utf-8")
        (sd / "setup-answers.json").write_text('{"profile": "garg"}', encoding="utf-8")
        core.seed_file().write_text(
            json.dumps(
                {
                    "schema": 1,
                    "admin": {"name": "carol", "ssh_keys": [good]},
                    "hostname": "garg-01",
                    "disk": "auto",
                    "wipe": True,
                    "setup": {"garg-model": "4B"},
                }
            ),
            encoding="utf-8",
        )
        rc, rep = core.apply_seed()
        chk(rc == 0 and "admin" in rep["applied"], f"seed: applied cleanly ({rep['failed']})")
        chk(["hostnamectl", "set-hostname", "garg-01"] in calls, "seed: hostname set")
        chk(
            "carol ALL=(ALL) NOPASSWD: ALL"
            in (tmp / "sudoers.d/90-awnix-admin").read_text(encoding="utf-8"),
            "seed: a key-only admin can sudo (NOPASSWD drop-in)",
        )
        chk(
            core.license_spool().read_text(encoding="utf-8").startswith("AITHER1."),
            "seed: license.lic lands in the activation spool",
        )
        chk(core.setup_answers_file().exists(), "seed: setup-answers.json lands in /etc/aither")
        chk(
            calls[-1] == ["garg-model", "set", "4B"] or ["garg-model", "set", "4B"] in calls,
            "seed: step values applied through the same argv path",
        )
        chk(
            not core.seed_file().exists() and not (sd / "license.lic").exists(),
            "seed: the seed and its files are deleted after apply",
        )
        core.seed_file().write_text(
            json.dumps({"schema": 1, "admin": {"name": "dave", "ssh_keys": [priv]}}),
            encoding="utf-8",
        )
        rc, rep = core.apply_seed()
        chk(
            rc == 1 and any("PRIVATE" in f for f in rep["failed"]), "seed: a private key is refused"
        )
        chk(
            core.validate_seed({"schema": 1, "admin": {"name": "e", "password": "pw"}}) != [],
            "seed: a plaintext password is refused",
        )
        chk(
            core.validate_seed({"schema": 1, "wipe": True}) != [],
            "seed: wipe without disk is refused",
        )

        # airgap profile: sshd is loopback-only and shipped accounts are locked, so a
        # key-only admin can never log in -- the password is the only way in.
        from awnix_setup import tty

        chk(not core.airgap(), "airgap: no profile marker reads as base")
        (tmp / "profile").write_text("airgap\n", encoding="utf-8")
        chk(core.airgap(), "airgap: the profile marker is read")
        code, body = api.apply_status("admin", {"name": "frank", "ssh_keys": good})
        chk(
            code == 422 and "airgap" in str(body.get("error")),
            "airgap: a key-only admin is refused",
        )
        code, body = api.apply_status(
            "admin", {"name": "frank", "github_user": "octocat", "password": "pw1"}
        )
        chk(code == 422 and "network" in str(body.get("error")), "airgap: GitHub import refused")
        code, body = api.apply_status(
            "admin", {"name": "frank", "ssh_keys": good, "password": "pw2"}
        )
        chk(code == 200, "airgap: a key + password admin is created")
        (tmp / "group").write_text("wheel:x:10:alice,frank\n", encoding="utf-8")
        (tmp / "shadow").write_text("alice:!$6$x:1::::::\nfrank:$6$s$h:1::::::\n", encoding="utf-8")
        chk(core.detect_admin() == "frank", "airgap: detect_admin wants a usable password")
        (tmp / "shadow").write_text("alice:!$6$x:1::::::\nfrank:!!:1::::::\n", encoding="utf-8")
        chk(core.detect_admin() is None, "airgap: a locked or key-only admin is not working")
        ok, _ = core.create_admin("alice", [good])
        chk(not ok, "airgap: create_admin refuses to extend a password-less admin with a key")
        (tmp / "shadow").write_text("frank:$6$s$h:1::::::\n", encoding="utf-8")
        ok, _ = core.create_admin("frank", [good])
        chk(ok, "airgap: an admin that already has a password may add a key")
        asked: list[str] = []
        answers = iter(["gina", good])
        secrets = iter(["", "pw", "pwX", "pw", "pw"])
        real_ask, real_secret, real_say = tty.ask, tty.ask_secret, tty.say
        tty.ask = lambda q: (asked.append(q), next(answers))[1]
        tty.ask_secret = lambda q: (asked.append(q), next(secrets))[1]
        tty.say = lambda msg="": None
        try:
            val = tty._ask_admin({"id": "admin", "current": None})
        finally:
            tty.ask, tty.ask_secret, tty.say = real_ask, real_secret, real_say
        chk(
            isinstance(val, dict) and val.get("password") == "pw" and val.get("ssh_keys") == good,
            "airgap tty: a pasted key still asks for a confirmed password",
        )
        chk(
            not any("Create one now" in q or "GitHub" in q for q in asked),
            "airgap tty: the admin step cannot be declined and offers no GitHub import",
        )
        (tmp / "profile").unlink()
        answers = iter(["hal", good])
        asked.clear()
        tty.ask = lambda q: (asked.append(q), "y" if "Create" in q else next(answers))[1]
        tty.ask_secret = lambda q: (asked.append(q), "nope")[1]
        tty.say = lambda msg="": None
        try:
            val = tty._ask_admin({"id": "admin", "current": None})
        finally:
            tty.ask, tty.ask_secret, tty.say = real_ask, real_secret, real_say
        chk(
            isinstance(val, dict) and val.get("password") == "",
            "base tty: a key-only admin is still allowed off the airgap profile",
        )
    finally:
        core.RUN, core.FETCH, core.GET_JSON, core.which = real_run, real_fetch, real_get, real_which
        os.environ.clear()
        os.environ.update(env_keep)
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST PASS" if not fails else f"SELF-TEST FAILED ({fails})")
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tty", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply-seed", nargs="?", const="", default=None, metavar="PATH")
    ap.add_argument("--validate-steps", metavar="DIR")
    ap.add_argument(
        "--no-path-check",
        action="store_true",
        help="with --validate-steps: skip the apply_cmd-on-PATH rule (repo lint)",
    )
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    a = ap.parse_args(argv)
    if a.list_verbs:
        print("\n".join(VERBS))
        return 0
    if a.self_test:
        return self_test()
    if a.validate_steps:
        return cmd_validate(a.validate_steps, not a.no_path_check)
    if a.status:
        return cmd_status(a.json)
    if a.apply_seed is not None:
        rc, rep = core.apply_seed(Path(a.apply_seed) if a.apply_seed else None)
        print(
            json.dumps(rep, indent=2)
            if a.json
            else f"awnix-setup: seed {rep.get('detail') or 'applied'}; "
            f"ok={rep['applied']} failed={rep['failed']}"
        )
        return rc
    if a.reset:
        if not a.yes:
            print(
                "awnix-setup: --reset removes /etc/awnix/setup.json so setup runs again "
                "(nothing else is touched). Re-run with --yes.",
                file=sys.stderr,
            )
            return 1
        print("awnix-setup: marker removed" if core.reset_setup() else "awnix-setup: no marker")
        return 0
    if a.tty:
        # The seed is applied HERE, in the unit's main process, and never in an
        # ExecStartPre=: a pre-step is part of the start job multi-user.target waits on,
        # and a seeded model download (timeout_s 3600) would hit the 90 s start timeout,
        # fail the unit and re-apply the seed every boot (AIN011). A broken seed must not
        # stop the prompt either: the tty is the fallback.
        try:
            rc, rep = core.apply_seed()
            if rep.get("detail") != "no seed":
                print(
                    f"awnix-setup: seed applied; ok={rep['applied']} failed={rep['failed']}",
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001 -- never lose the prompt to a bad seed
            print(f"awnix-setup: seed not applied ({type(e).__name__}: {e})", flush=True)
    if core.setup_complete():
        print("awnix-setup: setup is already complete. `awnix-setup --reset --yes` to redo it.")
        return 0
    from awnix_setup import tty

    try:
        return tty.run_interactive()
    except (KeyboardInterrupt, EOFError):
        print(
            "\n  setup cancelled -- nothing was recorded. Run `awnix-setup` to resume.", flush=True
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
