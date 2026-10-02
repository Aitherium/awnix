"""awnix-keys -- every key binding and core action, from ONE file.

    awnix-keys hyprland            the Hyprland bind lines (baked at image build into
                                   /usr/share/awnix/desktop/bindings.conf)
    awnix-keys menu                the searchable menu (Super+/): pick a line, it runs
    awnix-keys menu --print        the menu lines, one per entry, no launcher
    awnix-keys list [--json]       every entry, expanded
    awnix-keys run <id>            run one entry by id
    awnix-keys power               lock / log out / suspend / reboot / shut down
    awnix-keys validate [FILE]
    awnix-keys --self-test

The bindings file (/usr/share/awnix/desktop/bindings.toml) is the only place a key or
a core action is defined; the Hyprland config sources the generated bind lines and
binds nothing itself, and the menu reads the same file at run time -- so the menu
cannot list a key that does not work, or miss one that does.

Exit: 0 ok * 1 failed / violation * 2 could not judge (bindings file unreadable).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import TomlError, bindings_file, config_home, load_toml, loads_toml

#: Every core action the desktop promises. A bindings file without one of these is a
#: menu that cannot reach something the docs say it can.
CORE_ACTIONS = ("awsh", "menu", "launcher", "terminal", "browser", "console", "lock",
                "themes", "theme-next", "update", "component", "screenshot", "model")
VALID_FLAGS = set("lemrn")
MODS = {"SUPER", "SHIFT", "CTRL", "CONTROL", "ALT", "MOD2", "MOD3", "MOD5"}


#: Commands that hand their NEXT argument to a shell (`awnix-desktop-run '<script>'` is
#: `sh -c "$1"`). That argument is a second script inside the first one's quotes, so the
#: outer parse says nothing about it.
SHELL_WRAPPERS = ("awnix-desktop-run",)


class BindError(ValueError):
    """The bindings file is wrong. Exit 1."""


class NoShellError(RuntimeError):
    """No POSIX sh here, so no command could be parsed. Could not judge: exit 2."""


def _sh_syntax(script: str, sh: str) -> str:
    """'' when `sh -n` accepts the script, else the shell's own message."""
    try:
        r = subprocess.run([sh, "-n", "-c", script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NoShellError(f"cannot run {sh} -n: {exc}") from None
    if r.returncode == 0:
        return ""
    return (r.stderr.strip().splitlines() or [f"sh -n exit {r.returncode}"])[-1]


def nested_scripts(cmd: str) -> List[str]:
    """Every script a command hands to an inner shell: the argument after a
    SHELL_WRAPPERS command, and the argument after `sh -c` / `bash -c`."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return []          # unbalanced quotes: the outer `sh -n` reports it
    out: List[str] = []
    for i, tok in enumerate(toks[:-1]):
        name = tok.rsplit("/", 1)[-1]
        if name in SHELL_WRAPPERS:
            out.append(toks[i + 1])
        elif name in ("sh", "bash") and toks[i + 1] == "-c" and i + 2 < len(toks):
            out.append(toks[i + 2])
    return out


def shell_problems(entries: List[Dict[str, Any]], sh: Optional[str] = None) -> List[str]:
    """Every exec command that a shell refuses to parse, at either level.

    Hyprland's `exec` and the menu both run `sh -c <cmd>`. A command that does not
    parse is a key or a menu line that does nothing -- and one level down is where it
    hides: `awnix-desktop-run 'echo install <id>; exec bash'` parses (the quotes make
    it one word) and then dies inside the terminal with a syntax error.
    """
    exe = sh or shutil.which("sh")
    if not exe:
        raise NoShellError("no `sh` on PATH: the exec commands were not parsed")
    bad: List[str] = []
    for e in entries:
        if str(e.get("dispatch", "exec")) != "exec":
            continue
        cmd = str(e.get("cmd", ""))
        if not cmd.strip():
            continue
        eid = e.get("id")
        err = _sh_syntax(cmd, exe)
        if err:
            bad.append(f"{eid}: cmd is not valid shell: {err}")
            continue
        seen = [cmd]
        queue = nested_scripts(cmd)
        while queue:
            script = queue.pop(0)
            if script in seen:
                continue
            seen.append(script)
            err = _sh_syntax(script, exe)
            if err:
                bad.append(f"{eid}: the script it hands to an inner shell is not valid "
                           f"shell ({script!r}): {err}")
            else:
                queue.extend(nested_scripts(script))
    return bad


def _expand(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    rep = str(entry.get("repeat", "") or "")
    if not rep:
        return [dict(entry)]
    m = re.fullmatch(r"(\d+)-(\d+)", rep)
    if not m or int(m.group(1)) > int(m.group(2)):
        raise BindError(f"entry {entry.get('id')!r}: repeat must be N-M, got {rep!r}")
    out = []
    for n in range(int(m.group(1)), int(m.group(2)) + 1):
        e = {k: (v.replace("{n}", str(n)) if isinstance(v, str) else v)
             for k, v in entry.items() if k != "repeat"}
        e["_group_of"] = entry.get("id")
        out.append(e)
    return out


def normalise_keys(keys: str) -> str:
    """'SUPER SHIFT, T' -> 'SHIFT SUPER, t': a canonical form for duplicate detection."""
    mods, _, key = keys.partition(",")
    ms = sorted(m.upper().replace("CONTROL", "CTRL") for m in mods.split())
    return f"{' '.join(ms)}, {key.strip().lower()}"


def load_entries(path: Optional[Path] = None, text: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        data = loads_toml(text) if text is not None else load_toml(path or bindings_file())
    except TomlError as exc:
        raise BindError(str(exc)) from None
    raw = data.get("entry")
    if not isinstance(raw, list) or not raw:
        raise BindError("no [[entry]] tables")
    out: List[Dict[str, Any]] = []
    for e in raw:
        out.extend(_expand(e))
    return out


def problems(entries: List[Dict[str, Any]]) -> List[str]:
    """Everything wrong with a bindings file -- the checker and validate share this.
    Raises NoShellError when the commands could not be parsed at all (never a silent pass)."""
    bad: List[str] = shell_problems(entries)
    ids: Dict[str, int] = {}
    combos: Dict[str, str] = {}
    for e in entries:
        eid = str(e.get("id", "")).strip()
        if not eid:
            bad.append(f"an entry has no id: {e}")
            continue
        ids[eid] = ids.get(eid, 0) + 1
        for field in ("desc", "group"):
            if not str(e.get(field, "")).strip():
                bad.append(f"{eid}: no {field}")
        dispatch = str(e.get("dispatch", "exec"))
        if not re.fullmatch(r"[a-z]+", dispatch):
            bad.append(f"{eid}: dispatch {dispatch!r} is not a dispatcher name")
        if dispatch == "exec" and not str(e.get("cmd", "")).strip():
            bad.append(f"{eid}: exec with no cmd")
        if "#" in str(e.get("cmd", "")):
            bad.append(f"{eid}: cmd contains '#', which Hyprland reads as a comment")
        flags = str(e.get("flags", ""))
        if set(flags) - VALID_FLAGS:
            bad.append(f"{eid}: unknown bind flags {flags!r}")
        keys = e.get("keys")
        if keys is None:
            continue
        keys = str(keys)
        if keys.count(",") != 1 or not keys.split(",")[1].strip():
            bad.append(f"{eid}: keys must be 'MODS, KEY', got {keys!r}")
            continue
        for mod in keys.split(",")[0].split():
            if mod.upper() not in MODS:
                bad.append(f"{eid}: unknown modifier {mod!r}")
        norm = normalise_keys(keys)
        if norm in combos:
            bad.append(f"{eid}: {keys!r} is already bound by {combos[norm]}")
        combos[norm] = eid
    for eid, n in ids.items():
        if n > 1:
            bad.append(f"{eid}: id used {n} times")
    for core in CORE_ACTIONS:
        if core not in ids:
            bad.append(f"core action {core!r} is missing")
    return bad


def hyprland_lines(entries: List[Dict[str, Any]]) -> List[str]:
    lines = ["# GENERATED by awnix-keys from bindings.toml at image build -- do not edit.",
             "# Change a key in /usr/share/awnix/desktop/bindings.toml's source and rebuild,",
             "# or override it in ~/.config/hypr/awnix-user.conf (unbind, then bind)."]
    for e in entries:
        if e.get("keys") is None:
            continue
        flags = "".join(sorted(set(str(e.get("flags", "")))))
        cmd = str(e.get("cmd", ""))
        dispatch = str(e.get("dispatch", "exec"))
        line = f"bind{flags} = {e['keys']}, {dispatch}, {cmd}".rstrip()
        # A mouse bind takes no argument; every other bind keeps Hyprland's usual
        # trailing comma when its dispatcher has none (`killactive,`).
        lines.append(line.rstrip(", ") if "m" in flags else line)
    return lines


def _pretty_keys(keys: Optional[str]) -> str:
    if not keys:
        return ""
    mods, _, key = str(keys).partition(",")
    parts = [m.capitalize() for m in mods.split()] + [key.strip()]
    return " + ".join(p for p in parts if p)


def menu_lines(entries: List[Dict[str, Any]]) -> List[str]:
    """One line per entry, `desc  [keys]  (group)  #id`; repeated entries stay expanded
    so every generated binding has exactly one menu line (the checker counts them)."""
    out = []
    for e in entries:
        if e.get("menu") is False:
            continue
        k = _pretty_keys(e.get("keys"))
        out.append(f"{e['desc']}{'  [' + k + ']' if k else ''}  ({e['group']})  #{e['id']}")
    return out


def entry_by_id(entries: List[Dict[str, Any]], eid: str) -> Optional[Dict[str, Any]]:
    return next((e for e in entries if e.get("id") == eid), None)


def run_entry(e: Dict[str, Any]) -> int:
    dispatch = str(e.get("dispatch", "exec"))
    cmd = str(e.get("cmd", ""))
    if dispatch == "exec":
        argv = ["sh", "-c", cmd]
    else:
        argv = ["hyprctl", "dispatch", dispatch] + ([cmd] if cmd else [])
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        print(f"awnix-keys: cannot run {e.get('id')}: {exc}", file=sys.stderr)
        return 1
    return 0


def _fuzzel(lines: List[str], prompt: str) -> str:
    cfg = config_home() / "awnix" / "theme" / "fuzzel.ini"
    argv = ["fuzzel", "--dmenu", f"--prompt={prompt}", "--width=72"]
    if cfg.is_file():
        argv.insert(1, f"--config={cfg}")
    try:
        r = subprocess.run(argv, input="\n".join(lines), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip()


def menu(entries: List[Dict[str, Any]]) -> int:
    choice = _fuzzel(menu_lines(entries), "keys > ")
    m = re.search(r"#([A-Za-z0-9_-]+)$", choice)
    if not m:
        return 1
    e = entry_by_id(entries, m.group(1))
    return run_entry(e) if e else 1


POWER = [
    ("Lock", "awnix-lock"),
    ("Log out", "hyprctl dispatch exit"),
    ("Suspend", "systemctl suspend"),
    ("Reboot", "systemctl reboot"),
    ("Shut down", "systemctl poweroff"),
]


def power() -> int:
    choice = _fuzzel([p for p, _ in POWER], "power > ")
    for label, cmd in POWER:
        if choice == label:
            return run_entry({"id": label, "cmd": cmd})
    return 1


def self_test() -> int:
    fails = 0

    def chk(cond: bool, label: str) -> None:
        nonlocal fails
        print(f"  {'ok  ' if cond else 'FAIL'} {label}")
        if not cond:
            fails += 1

    src = bindings_file()
    if not src.is_file():
        src = Path(__file__).resolve().parent.parent / "bindings.toml"
    try:
        entries = load_entries(src)
    except BindError as exc:
        print(f"awnix-keys self-test: cannot judge {src}: {exc}")
        return 2
    try:
        chk(problems(entries) == [], f"the shipped bindings file is clean ({src.name})")
    except NoShellError as exc:
        print(f"awnix-keys self-test: cannot judge: {exc}")
        return 2
    text = src.read_text(encoding="utf-8")
    try:
        import tomllib
        chk(loads_toml(text, force_mini=True) == tomllib.loads(text),
            "mini TOML reader == tomllib on the bindings file")
    except ImportError:
        chk(bool(loads_toml(text, force_mini=True).get("entry")), "mini TOML reader parses it")
    hl = [ln for ln in hyprland_lines(entries) if ln.startswith("bind")]
    keyed = [e for e in entries if e.get("keys") is not None]
    chk(len(hl) == len(keyed), "one Hyprland bind line per keyed entry")
    ml = menu_lines(entries)
    menu_ids = {ln.rsplit("#", 1)[1] for ln in ml}
    chk(all(e["id"] in menu_ids for e in keyed if e.get("menu") is not False),
        "every key appears in the menu")
    chk(all(c in menu_ids for c in CORE_ACTIONS), "every core action appears in the menu")
    chk(any(ln.startswith("bind = SUPER, A, exec,") and "awsh" in ln for ln in hl),
        "Super+A opens awsh")
    chk(any(ln.startswith("bind = SUPER, slash, exec, awnix-keys menu") for ln in hl),
        "Super+/ opens this menu")
    chk(sum(1 for e in entries if str(e.get("id", "")).startswith("workspace-")) == 9,
        "repeat expands 1-9")

    def bad(snippet: str, needle: str, label: str) -> None:
        base = "\n".join(f'[[entry]]\nid = "{c}"\ndesc = "d"\ngroup = "g"\ncmd = "true"'
                         for c in CORE_ACTIONS)
        try:
            p = problems(load_entries(text=base + "\n" + snippet))
        except BindError as exc:
            p = [str(exc)]
        chk(any(needle in x for x in p), label)

    bad('[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\nkeys = "SUPER, A"\ncmd = "a"\n'
        '[[entry]]\nid = "y"\ndesc = "d"\ngroup = "g"\nkeys = "SUPER, a"\ncmd = "b"',
        "already bound", "a key bound twice is refused (case-insensitive)")
    bad('[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\nkeys = "HYPER, A"\ncmd = "a"',
        "unknown modifier", "an unknown modifier is refused")
    bad('[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\ncmd = "a # b"',
        "comment", "a '#' in a command is refused")
    bad('[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\ncmd = "echo \'unclosed"',
        "not valid shell", "a command the shell cannot parse is refused")
    bad('[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\n'
        'cmd = "foot -e awnix-desktop-run \'echo install <id>; exec bash\'"',
        "inner shell", "a broken script inside awnix-desktop-run's quotes is refused")
    bad('[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\n'
        'cmd = "foot -e sh -c \'if true; then echo\'"',
        "inner shell", "a broken script inside sh -c is refused")
    fine = load_entries(text='[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\n'
                             'cmd = "foot -e awnix-desktop-run \'echo install ID; exec bash\'"')
    chk(shell_problems(fine) == [], "the same script with a plain word passes")
    try:
        p = problems(load_entries(text='[[entry]]\nid = "x"\ndesc = "d"\ngroup = "g"\ncmd = "a"'))
    except BindError as exc:
        p = [str(exc)]
    chk(any("core action 'awsh'" in x for x in p), "a missing core action is refused")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "b.toml"
        f.write_text("[[entry]\nid = x\n", encoding="utf-8")
        try:
            load_entries(f)
            chk(False, "a malformed file is refused")
        except BindError:
            chk(True, "a malformed file is refused")
    print(f"awnix-keys self-test: {'PASS' if not fails else f'FAIL ({fails})'}")
    return 0 if not fails else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnix-keys", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list-verbs", action="store_true")
    ap.add_argument("--file", default=None, help="bindings file (default: the installed one)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("hyprland")
    m = sub.add_parser("menu")
    m.add_argument("--print", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("id")
    sub.add_parser("power")
    v = sub.add_parser("validate")
    v.add_argument("path", nargs="?")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.list_verbs:
        print("\n".join(["hyprland", "menu", "list", "run", "power", "validate"]))
        return 0
    if a.cmd == "power":
        return power()
    path = Path(a.file) if a.file else None
    if a.cmd == "validate" and a.path:
        path = Path(a.path)
    try:
        entries = load_entries(path)
    except BindError as exc:
        print(f"awnix-keys: {exc} -- NOT VERIFIED", file=sys.stderr)
        return 2
    if a.cmd == "hyprland":
        print("\n".join(hyprland_lines(entries)))
        return 0
    if a.cmd == "menu":
        if a.print:
            print("\n".join(menu_lines(entries)))
            return 0
        return menu(entries)
    if a.cmd == "list":
        if a.json:
            print(json.dumps([{k: v for k, v in e.items() if not k.startswith("_")}
                              for e in entries], indent=2))
        else:
            for e in entries:
                print(f"{e['id']:<18} {_pretty_keys(e.get('keys')):<26} {e['desc']}")
        return 0
    if a.cmd == "run":
        e = entry_by_id(entries, a.id)
        if not e:
            print(f"awnix-keys: no entry {a.id!r}", file=sys.stderr)
            return 1
        return run_entry(e)
    if a.cmd == "validate":
        try:
            p = problems(entries)
        except NoShellError as exc:
            print(f"awnix-keys: {exc} -- NOT VERIFIED", file=sys.stderr)
            return 2
        for x in p:
            print(f"awnix-keys: {x}", file=sys.stderr)
        return 1 if p else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
