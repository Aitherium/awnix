"""Pure setup handlers that awnix-console mounts under /api/setup/*.

No HTTP lives here. The console mounts the four PUBLIC handlers and sends what they
return as an HTTP 200 body (awnix_console/server.py ``self._json(200, api.steps())``):

    GET  /api/setup/steps          -> steps()          -> dict
    GET  /api/setup/state          -> state()          -> dict
    POST /api/setup/steps/{id}     -> apply(id, body["value"]) -> dict
    POST /api/setup/finish         -> finish()         -> dict

So a public handler returns a plain dict on success and RAISES on failure; it never
returns a status tuple (a tuple would be serialised as a JSON array under a 200, and a
failed apply would read as success). The console maps ValueError/KeyError -> 422 and
PermissionError -> 403; every exception here also carries ``.status`` (404 unknown step,
409 the apply ran and failed, 410 setup already complete, 422 rejected before anything
ran) and ``.body`` for a console that honours them.

The tty and the self-test use the ``*_status`` twins, which return (http_status, body).
A secret value is never returned, logged or recorded; the reveal output is returned
exactly once.
"""
from __future__ import annotations

import re
import socket
from typing import Any

from . import core


class SetupApiError(ValueError):
    """A setup handler failed. ``status`` is the HTTP status it means; ``body`` the JSON."""

    def __init__(self, status: int, body: dict):
        super().__init__(str(body.get("error") or f"setup failed ({status})"))
        self.status = status
        self.body = body


class StepNotFoundError(SetupApiError, KeyError):
    """404: no such step on this image (a KeyError too, as the console's stub raises)."""

    def __str__(self) -> str:  # KeyError would quote the message
        return str(self.body.get("error") or "no such step")


class SetupCompleteError(PermissionError):
    """410: setup already finished; the console serves console mode from here on."""

    def __init__(self, body: dict):
        super().__init__(str(body.get("error") or "setup is complete"))
        self.status = 410
        self.body = body


def _unwrap(res: tuple[int, dict]) -> dict:
    status, body = res
    if 200 <= status < 300:
        return body
    if status == 410:
        raise SetupCompleteError(body)
    if status == 404:
        raise StepNotFoundError(status, body)
    raise SetupApiError(status, body)



#: Builtin steps take these order slots; steps.d files use their NN prefix.
_BUILTIN_ORDER = {"hostname": 10, "admin": 20, "components": 30, "link": 80}


def _builtin_steps() -> list[dict]:
    menu = core.component_menu()
    admin = core.detect_admin()
    comp: dict[str, Any]
    if menu:
        comp = {"id": "components", "title": "Optional components",
                "why": "The core aw* tools are already installed. Pick extras to add now; "
                       "`awnix component` adds or removes them any time, with rollback.",
                "kind": "choice", "multi": True, "options": menu}
    else:
        comp = {"id": "components", "title": "Optional components",
                "why": "Nothing extra is offered for this image right now. "
                       "`awnix component list` shows what exists and why.",
                "kind": "info", "text": "No optional components to install."}
    return [
        {"id": "hostname", "title": "Name this machine",
         "why": "How it shows up on your network and in your fleet.",
         "kind": "text", "current": socket.gethostname()},
        {"id": "admin", "title": "Administrator account",
         "why": "The account you log in with over SSH. A key is safer than a password; "
                "paste your .pub key or import the keys on your GitHub account.",
         "kind": "text", "current": admin,
         "fields": [
             {"name": "name", "label": "Username", "kind": "text"},
             {"name": "ssh_keys", "label": "SSH public key(s), one per line",
              "kind": "text", "multiline": True},
             {"name": "github_user", "label": "...or import keys from GitHub user",
              "kind": "text"},
             {"name": "password", "label": "Password (optional)", "kind": "secret"},
         ]},
        comp,
        {"id": "link", "title": "Link to your Aitherium fleet",
         "why": "Optional. Skipping leaves a fully working standalone box; you can link "
                "later with `awnix setup`.",
         "kind": "toggle"},
    ]


def _order(step: dict) -> int:
    if step["id"] in _BUILTIN_ORDER and step.get("builtin"):
        return _BUILTIN_ORDER[step["id"]]
    m = re.match(r"^(\d+)-", step.get("_file", ""))
    return int(m.group(1)) if m else 99


def all_steps() -> list[dict]:
    out = []
    for s in _builtin_steps():
        s["builtin"] = True
        out.append(s)
    extra, _ = core.load_steps()
    builtin_ids = {s["id"] for s in out}
    for s in extra:
        if s["id"] in builtin_ids:
            continue  # a steps.d file may not shadow a builtin
        out.append(s)
    return sorted(out, key=_order)


def _public(step: dict, progress: dict) -> dict:
    view = {k: v for k, v in step.items()
            if not k.startswith("_") and k not in ("apply_cmd", "options_cmd", "current_cmd",
                                                   "reveal_cmd", "secret_dest")}
    if not step.get("builtin"):
        if step["kind"] == "choice":
            view["options"] = core.step_options(step)
        cur = core.step_current(step)
        if cur is not None:
            view["current"] = cur
        if "text" in view:
            view["text"] = core.render_text(view["text"])
        if "url" in view:
            view["url"] = core.render_text(view["url"])
    view["status"] = (progress.get("steps") or {}).get(step["id"], {}).get("status", "pending")
    view.setdefault("required", False)
    return view


def steps_status() -> tuple[int, dict]:
    if core.setup_complete():
        return 410, {"error": "setup is complete", "complete": True}
    prog = core.read_progress()
    return 200, {"variant": core.variant(), "steps": [_public(s, prog) for s in all_steps()]}


def state_status() -> tuple[int, dict]:
    prog = core.read_progress()
    return 200, {
        "complete": core.setup_complete(),
        "state": core.redacted_state() or None,
        "progress": {k: prog.get(k) for k in ("steps", "hostname", "admin_user",
                                                "ssh_key_fingerprints", "installed",
                                                "linked") if k in prog},
    }


def _apply_admin(value: Any) -> tuple[int, dict]:
    if not isinstance(value, dict):
        return 422, {"error": "admin takes {name, ssh_keys, github_user?, password?}"}
    keys_raw = value.get("ssh_keys") or ""
    keys = core.parse_keys_blob(keys_raw) if isinstance(keys_raw, str) else list(keys_raw)
    for k in keys:
        if core.looks_private(k):
            return 422, {"error": "that is a PRIVATE key -- paste the .pub half only"}
    gh = (value.get("github_user") or "").strip()
    if gh and core.airgap():
        return 422, {"error": "GitHub key import needs the network; the airgap profile has none "
                              "-- paste a key and set a password"}
    if gh:
        got, why = core.import_github_keys(gh)
        if not got:
            return 422, {"error": why}
        keys += got
    ok, res = core.create_admin(str(value.get("name") or ""), keys,
                                password=value.get("password") or None)
    if not ok:
        core.mark_step("admin", "failed")
        return 422, {"error": res.get("error")}
    core.mark_step("admin", "done", admin_user=res["admin_user"],
                   ssh_key_fingerprints=res["ssh_key_fingerprints"])
    return 200, {"ok": True, "admin_user": res["admin_user"],
                 "ssh_key_fingerprints": res["ssh_key_fingerprints"]}


def _apply_link(value: Any) -> tuple[int, dict]:
    if value in (False, None, "off", "skip") or (isinstance(value, dict)
                                                 and value.get("action") == "skip"):
        core.mark_step("link", "skipped", linked=False)
        return 200, {"ok": True, "linked": False}
    action = value.get("action") if isinstance(value, dict) else "start"
    if action == "start":
        try:
            d = core.start_device_flow()
        except Exception as e:
            return 409, {"error": f"could not start the link: {e}"}
        return 200, {"ok": True, "pending": True,
                     "verification_uri": d.get("verification_uri_complete")
                     or d.get("verification_uri"),
                     "user_code": d.get("user_code"), "device_code": d.get("device_code"),
                     "interval": int(d.get("interval", 5)),
                     "expires_in": int(d.get("expires_in", 900))}
    if action == "poll":
        code = str((value or {}).get("device_code") or "")
        if not code:
            return 422, {"error": "poll needs device_code"}
        st, val = core.poll_once(code)
        if st == "linked":
            core.write_link_token(val)
            core.mark_step("link", "done", linked=True)
            return 200, {"ok": True, "linked": True}
        if st in ("pending", "slow_down"):
            return 200, {"ok": True, "pending": True, "slow_down": st == "slow_down"}
        core.mark_step("link", "failed", linked=False)
        return 409, {"error": val}
    return 422, {"error": "link takes false, {action:start} or {action:poll, device_code}"}


def apply_status(step_id: str, value: Any) -> tuple[int, dict]:
    if core.setup_complete():
        return 410, {"error": "setup is complete"}
    if step_id == "hostname":
        ok, msg = core.set_hostname(str(value or ""))
        core.mark_step("hostname", "done" if ok else "failed",
                       **({"hostname": msg} if ok else {}))
        return (200, {"ok": True, "hostname": msg}) if ok else (422, {"error": msg})
    if step_id == "admin":
        return _apply_admin(value)
    if step_id == "components":
        ids = value if isinstance(value, list) else ([] if value in (None, "", False)
                                                       else [value])
        if not all(isinstance(i, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", i)
                   for i in ids):
            return 422, {"error": "component ids only"}
        ok, res = core.install_components(ids)
        core.mark_step("components", "done" if ok else "failed",
                       installed=res.get("installed", []))
        return (200, {"ok": True, **res}) if ok else (409, {"error": res.get("error")})
    if step_id == "link":
        return _apply_link(value)
    by_id = {s["id"]: s for s in all_steps() if not s.get("builtin")}
    st = by_id.get(step_id)
    if not st:
        return 404, {"error": f"no step '{step_id}' on this image"}
    if value == "__skip__":
        core.mark_step(step_id, "skipped")
        return 200, {"ok": True, "skipped": True}
    ok, msg = core.run_step(st, value)
    core.mark_step(step_id, "done" if ok else "failed")
    if st["kind"] == "reveal":
        # Shown ONCE. Not stored; a second call re-runs reveal_cmd, which a well-made
        # reveal (aither-setup --reveal-once) refuses.
        return (200, {"ok": True, "reveal": msg}) if ok else (409, {"error": msg})
    if st["kind"] == "secret":
        return (200, {"ok": True}) if ok else (409, {"error": msg})
    return (200, {"ok": True, "detail": msg}) if ok else (409, {"error": msg})


def finish_status() -> tuple[int, dict]:
    if core.setup_complete():
        return 410, {"error": "setup is complete"}
    st = core.finish_setup(stop_tty=True)
    return 200, {"ok": True, "state": st, "next": "console"}


# ── the mount contract: dict on success, raise on failure ───────────────────────────────
def steps() -> dict:
    return _unwrap(steps_status())


def state() -> dict:
    return _unwrap(state_status())


def apply(step_id: str, value: Any) -> dict:
    return _unwrap(apply_status(step_id, value))


def finish() -> dict:
    return _unwrap(finish_status())
