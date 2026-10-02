"""The tty1 first-boot flow. Same handlers as the web console (awnix_setup.api).

Every question is declinable: awnix's identity is "No services. No agent. No account."
The one exception is the airgap profile with no working administrator: there the admin
and its password are required, because a key-only admin can never log in.
The unit that runs this is Type=simple and never holds multi-user.target, so a headless
box (garg on a shelf) boots, serves, and waits here without anyone answering.
"""
from __future__ import annotations

import getpass
import json
from typing import Any, Callable

from . import api, core

say: Callable[..., None] = lambda msg="": print(msg, flush=True)  # noqa: E731
ask: Callable[[str], str] = input
ask_secret: Callable[[str], str] = getpass.getpass


def banner_lines() -> list[str]:
    """The web console URL and code, from `awnix console url` (console-surfaces owns it)."""
    rc, out, _ = core.run([core.AWNIX_BIN, "console", "url"], timeout=10)
    if rc == 0 and out.strip():
        return [ln for ln in out.strip().splitlines()]
    return ["(the web console is not running yet -- `awnix console url` prints it later)"]


def _ask_password() -> str:
    """Ask twice until two non-empty entries match. Only the airgap flow calls this."""
    while True:
        pw = ask_secret("  Password (required): ")
        if not pw:
            say("  A password is required on this box.")
            continue
        if ask_secret("  Password again: ") == pw:
            return pw
        say("  The two entries differ; try again.")


def _ask_admin(step: dict) -> Any:
    air = core.airgap()
    cur = step.get("current")
    if cur:
        say(f"  An administrator already exists: {cur}.")
        if not ask("  Add another? [y/N]: ").strip().lower().startswith("y"):
            return None
    elif air:
        # awnix-zero-ports locked every shipped account and bound sshd to 127.0.0.1, so a
        # key-only admin could never log in and setup never runs again (a rescue boot).
        say("  airgap profile: SSH listens on this box only (127.0.0.1), so the")
        say("  administrator's PASSWORD at this console is the only way to log in")
        say("  and the only recovery login. This step cannot be skipped.")
    else:
        say("  No administrator can log in over SSH yet. Strongly recommended.")
        if ask("  Create one now? [Y/n]: ").strip().lower().startswith("n"):
            return None
    name = ask("  Username: ").strip()
    say("  Paste one SSH public key (ssh-ed25519 AAAA...), or leave empty.")
    key = ask("  Key: ").strip()
    gh = ""
    if not key and not air:
        gh = ask("  ...or import keys from GitHub user (empty to skip): ").strip()
    pw = ""
    if air:
        pw = _ask_password()
    elif not key and not gh:
        pw = ask_secret("  Password (no key given): ")
    return {"name": name, "ssh_keys": key, "github_user": gh, "password": pw}


def _ask_link() -> None:
    if not ask("  Link now? [y/N]: ").strip().lower().startswith("y"):
        api.apply_status("link", False)
        return
    code, d = api.apply_status("link", {"action": "start"})
    if code != 200:
        say(f"    could not start the link: {d.get('error')}")
        return
    say(f"    Go to:  {d.get('verification_uri')}")
    say(f"    Code :  {d.get('user_code')}")
    say("    Waiting for approval (Ctrl-C to skip)...")
    try:
        ok, res = core.poll_device_flow(d["device_code"], d["interval"], d["expires_in"])
    except KeyboardInterrupt:
        say("\n    skipped.")
        api.apply_status("link", False)
        return
    if ok:
        core.write_link_token(res)
        core.mark_step("link", "done", linked=True)
        say("    linked.")
    else:
        core.mark_step("link", "failed", linked=False)
        say(f"    not linked: {res}")


def _ask_step(step: dict) -> None:
    kind = step["kind"]
    say(f"  {step['title']}")
    say(f"  {step['why']}")
    if kind == "info":
        say("  " + (step.get("text") or ""))
        if step.get("url"):
            say(f"  {step['url']}")
        api.apply_status(step["id"], None)
        return
    if kind == "choice":
        opts = step.get("options") or []
        for i, o in enumerate(opts, 1):
            mark = " *" if o.get("active") else ""
            say(f"    {i}. {o['label']:<14}{o.get('detail', '')}{mark}")
        if step.get("multi"):
            raw = ask("  Pick numbers (e.g. 1 3), 'all', or Enter for none: ")
            val: Any = core.parse_selection(raw, opts)
        else:
            raw = ask("  Pick one (Enter keeps the current): ").strip()
            picked = core.parse_selection(raw, opts)
            if not picked:
                api.apply_status(step["id"], "__skip__")
                return
            val = picked[0]
    elif kind == "toggle":
        val = ask("  Turn on? [y/N]: ").strip().lower().startswith("y")
    elif kind == "secret":
        val = ask_secret("  Value (hidden, Enter to skip): ")
        if not val:
            api.apply_status(step["id"], "__skip__")
            return
    elif kind == "reveal":
        if not ask("  Show it now? It is displayed ONCE. [y/N]: ").strip().lower().startswith("y"):
            api.apply_status(step["id"], "__skip__")
            return
        code, d = api.apply_status(step["id"], None)
        if code == 200:
            say("  " + "-" * 60)
            say(d.get("reveal", ""))
            say("  " + "-" * 60)
            while ask("  Type 'saved' once you have stored it: ").strip().lower() != "saved":
                pass
        else:
            say(f"  could not reveal: {d.get('error')}")
        return
    else:
        cur = step.get("current")
        raw = ask(f"  Value{f' [{cur}]' if cur else ''} (Enter to skip): ").strip()
        if not raw:
            api.apply_status(step["id"], "__skip__")
            return
        val = raw
    code, d = api.apply_status(step["id"], val)
    say("  done." if code == 200 else f"  not applied: {d.get('error')}")


def run_interactive() -> int:
    say()
    say("=" * 68)
    say("  awnix -- first boot")
    if core.airgap():
        say("  Every question is optional except the administrator (airgap profile).")
        say("  You can also finish setup in a browser on this box:")
    else:
        say("  Every question is optional. You can also finish setup in a browser:")
    for ln in banner_lines():
        say("    " + ln)
    say("=" * 68)
    say()
    for step in api.all_steps():
        sid = step["id"]
        if sid == "hostname":
            cur = step.get("current")
            raw = ask(f"  Hostname [{cur}]: ").strip()
            if raw and raw != cur:
                code, d = api.apply_status("hostname", raw)
                say(f"  hostname -> {d.get('hostname')}" if code == 200
                    else f"  could not set hostname: {d.get('error')}")
        elif sid == "admin":
            # On the airgap profile with no working admin, loop until one exists: finishing
            # here writes setup.json and this prompt never comes back.
            must = core.airgap() and not step.get("current")
            while True:
                val = _ask_admin(step)
                if val:
                    code, d = api.apply_status("admin", val)
                    say(f"  admin '{d.get('admin_user')}' ready." if code == 200
                        else f"  not created: {d.get('error')}")
                    if code == 200:
                        break
                if not must:
                    break
                say("  This box cannot finish setup without an administrator; try again.")
        elif sid == "link":
            say("  Link this machine to your Aitherium fleet?")
            say("  " + step["why"])
            _ask_link()
        else:
            view = api._public(step, core.read_progress())
            _ask_step(view)
        say()
    st = core.finish_setup()
    say("=" * 68)
    say(f"  Setup complete. Choices recorded in {core.state_file()}")
    say("  Re-run any time:  sudo awnix setup --reset --yes  (then reboot)")
    say("=" * 68)
    say(json.dumps({"admin_user": st.get("admin_user"), "installed": st.get("installed")}))
    return 0
