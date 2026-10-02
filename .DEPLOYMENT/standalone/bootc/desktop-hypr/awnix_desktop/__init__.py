"""awnix desktop -- shared pieces of awnix-theme and awnix-keys.

Installed at /usr/lib/awnix-desktop/awnix_desktop and run by /usr/bin/python3.11 on the
image; imported from the repo by AitherOS/dev/tools/check_awnix_desktop.py, which must
also run on python 3.10. So: stdlib only, and `tomllib` is optional -- the reader below
handles exactly the TOML subset the themes and the bindings file use, and the self-tests
prove it agrees with tomllib wherever tomllib exists.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "1"

#: The installed layout. Every path is overridable so the self-tests and the repo
#: checker run against the source tree without installing anything.
SHARE = Path(os.environ.get("AWNIX_DESKTOP_SHARE", "/usr/share/awnix"))


def themes_dir() -> Path:
    return Path(os.environ.get("AWNIX_THEMES_DIR", str(SHARE / "themes")))


def templates_dir() -> Path:
    return Path(os.environ.get("AWNIX_THEME_TEMPLATES", str(SHARE / "theme-templates")))


def bindings_file() -> Path:
    return Path(os.environ.get("AWNIX_BINDINGS", str(SHARE / "desktop" / "bindings.toml")))


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base)


class TomlError(ValueError):
    """The file is outside the supported subset, or malformed. Never guessed around."""


# ── a small TOML reader ────────────────────────────────────────────────────────────


def _parse_string(s: str, i: int) -> tuple:
    q = s[i]
    j = i + 1
    out: List[str] = []
    while j < len(s):
        c = s[j]
        if c == q:
            return "".join(out), j + 1
        if q == '"' and c == "\\":
            j += 1
            if j >= len(s):
                break
            esc = s[j]
            table = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
            if esc not in table:
                raise TomlError(f"unsupported escape \\{esc}")
            out.append(table[esc])
        else:
            out.append(c)
        j += 1
    raise TomlError("unterminated string")


def _parse_value(s: str) -> tuple:
    """Returns (value, rest-of-line)."""
    s = s.lstrip()
    if not s:
        raise TomlError("missing value")
    if s[0] in "\"'":
        if s.startswith('"""') or s.startswith("'''"):
            raise TomlError("multi-line strings are not supported")
        v, j = _parse_string(s, 0)
        return v, s[j:]
    if s[0] == "[":
        items: List[Any] = []
        rest = s[1:].lstrip()
        while True:
            if not rest:
                raise TomlError("unterminated array (arrays must fit on one line)")
            if rest[0] == "]":
                return items, rest[1:]
            v, rest = _parse_value(rest)
            items.append(v)
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest.startswith("]"):
                raise TomlError("expected , or ] in array")
    m = re.match(r"[^\s,\]#]+", s)
    token = m.group(0) if m else ""
    rest = s[m.end():] if m else s
    if token in ("true", "false"):
        return token == "true", rest
    if re.fullmatch(r"[+-]?\d[\d_]*", token):
        return int(token.replace("_", "")), rest
    if re.fullmatch(r"[+-]?\d+\.\d+", token):
        return float(token), rest
    raise TomlError(f"unsupported value {token!r}")


def _strip_comment(rest: str) -> str:
    rest = rest.strip()
    if rest and not rest.startswith("#"):
        raise TomlError(f"trailing content {rest!r}")
    return ""


def _mini_toml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    cur: Dict[str, Any] = root
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("[["):
                end = line.index("]]")
                _strip_comment(line[end + 2:])
                name = line[2:end].strip()
                parent = root
                parts = name.split(".")
                for p in parts[:-1]:
                    parent = parent.setdefault(p.strip(), {})
                arr = parent.setdefault(parts[-1].strip(), [])
                if not isinstance(arr, list):
                    raise TomlError(f"{name} is both a table and an array")
                cur = {}
                arr.append(cur)
                continue
            if line.startswith("["):
                end = line.index("]")
                _strip_comment(line[end + 1:])
                cur = root
                for p in line[1:end].split("."):
                    nxt = cur.setdefault(p.strip(), {})
                    if not isinstance(nxt, dict):
                        raise TomlError(f"{p} is not a table")
                    cur = nxt
                continue
            if "=" not in line:
                raise TomlError("expected key = value")
            key, _, val = line.partition("=")
            key = key.strip()
            if not key or any(c in key for c in " \"'"):
                raise TomlError(f"unsupported key {key!r}")
            v, rest = _parse_value(val)
            _strip_comment(rest)
            if key in cur:
                raise TomlError(f"duplicate key {key!r}")
            cur[key] = v
        except (TomlError, ValueError) as exc:
            raise TomlError(f"line {n}: {exc}") from None
    return root


def loads_toml(text: str, *, force_mini: bool = False) -> Dict[str, Any]:
    if not force_mini:
        try:
            import tomllib  # python >= 3.11
        except ImportError:
            tomllib = None  # type: ignore[assignment]
        if tomllib is not None:
            try:
                return tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise TomlError(str(exc)) from None
    return _mini_toml(text)


def load_toml(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TomlError(f"cannot read {path}: {exc}") from None
    return loads_toml(text)


def which(cmd: str) -> Optional[str]:
    import shutil
    return shutil.which(cmd)
