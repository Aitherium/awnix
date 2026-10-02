#!/usr/bin/env python3
"""awman -- render the awnix admin docs from ONE source into man, HTML and guide.json.

The sources are Markdown files with a YAML-lite front matter block, listed in
manifest.yaml next to this file. This renderer is the only doc toolchain the image
build runs: stdlib only, Python 3.10-compatible, no pandoc, no scdoc, no PyYAML.

    awman.py render --roff OUT --html OUT --json OUT [--variant V | --public] [--src DIR]
    awman.py lint [--src DIR]
    awman.py --self-test

Outputs
  roff   OUT/man<section>/<name>.<section>    (uncompressed man(7))
  html   OUT/<chapter-id>.html, OUT/<name>.<section>.html, OUT/index.html
  json   guide.json, schema v1:
         {version, source_sha256, variants[], public, chapters[{id,title,applies_to[],
          html,headings[{id,text,level,variants[]}]}], man[{name,section,summary,
          applies_to[],status,html,verbs[]}]}

Install locations in every image (the docs-output contract): roff into /usr/share/man,
HTML into /usr/share/doc/awnix/html, and the guide into /usr/share/doc/awnix/guide.json,
which the web console serves at /guide.json.

Every output is deterministic (sorted, no timestamps), so a freshness diff can fail.

Variant scoping. A heading may end in `{variants: a, b}`; the section it opens (to the
next heading of the same or a higher level) then applies only to those variants.
`--variant V` keeps only what applies to V. `--public` keeps only the manifest's
`public:` variants and drops every chapter, page and section with none of them, which
is how the public web copy carries no private-variant content.

The Markdown subset is deliberately small: `##`/`###` headings, paragraphs, fenced code,
`-` bullets, `1.` ordered items, definition lists (`term` then `: definition`), `> `
notes, and inline `code`, **bold** and [links](url). Anything else (tables, `#`
headings, nested lists) is a lint error, never silently wrong roff.

Exit: 0 ok, 1 lint violation / self-test failure, 2 could not read the sources.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
MAN_REQUIRED = ("SYNOPSIS", "DESCRIPTION", "FILES", "SEE ALSO")
MAN_KEYS = ("name", "section", "summary", "applies_to", "status")
CHAPTER_KEYS = ("id", "title", "applies_to")
STATUSES = ("live", "pending-cli")
_SCOPE_RE = re.compile(r"\s*\{variants:\s*([^}]*)\}\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^- (.*)$")
_ORDERED_RE = re.compile(r"^(\d+)\. (.*)$")


class DeadError(RuntimeError):
    """The sources cannot be read. Exit 2."""


class LintError(ValueError):
    """A source uses something outside the supported subset. Exit 1."""


# ── front matter ───────────────────────────────────────────────────────────────────


def _scalar(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_unquote(v.strip()) for v in inner.split(",") if v.strip()]
    return _unquote(value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_yaml_lite(text: str) -> dict:
    """`key: scalar`, `key: [a, b]`, a `key:` followed by `  - item` lines, and a
    `key:` followed by `  sub: value` lines (one level). Comments start with `#`."""
    out: dict = {}
    cur_key: str | None = None
    for lineno, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 0:
            if ":" not in body:
                raise LintError(f"line {lineno}: expected `key: value`, got {body!r}")
            key, _, value = body.partition(":")
            key = key.strip()
            if value.strip():
                out[key] = _scalar(value)
                cur_key = None
            else:
                out[key] = None
                cur_key = key
            continue
        if cur_key is None:
            raise LintError(f"line {lineno}: indented line with no open key: {body!r}")
        if body.startswith("- "):
            if out[cur_key] is None:
                out[cur_key] = []
            if not isinstance(out[cur_key], list):
                raise LintError(f"line {lineno}: `{cur_key}` mixes a list and a mapping")
            out[cur_key].append(_unquote(body[2:].strip()))
        elif ":" in body:
            if out[cur_key] is None:
                out[cur_key] = {}
            if not isinstance(out[cur_key], dict):
                raise LintError(f"line {lineno}: `{cur_key}` mixes a list and a mapping")
            sub, _, value = body.partition(":")
            out[cur_key][sub.strip()] = _scalar(value)
        else:
            raise LintError(f"line {lineno}: cannot parse {body!r}")
    return out


def split_front_matter(text: str) -> tuple[dict, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise LintError("missing front matter (the file must start with `---`)")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise LintError("front matter is not closed with `---`")
    return parse_yaml_lite(text[4:end]), text[end + 5 :]


# ── block parser ───────────────────────────────────────────────────────────────────


def parse_blocks(body: str) -> list[dict]:
    """Markdown subset -> a flat block list. Raises LintError on anything else."""
    lines = body.split("\n")
    blocks: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            j = i + 1
            code: list[str] = []
            while j < n and lines[j].strip() != "```":
                code.append(lines[j])
                j += 1
            if j >= n:
                raise LintError(f"unclosed code fence opened at body line {i + 1}")
            blocks.append({"t": "code", "lang": lang, "text": "\n".join(code)})
            i = j + 1
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if level not in (2, 3):
                raise LintError(
                    f"body line {i + 1}: only ## and ### headings are supported "
                    f"(the title comes from the front matter): {line!r}"
                )
            text = m.group(2).strip()
            variants: list[str] = []
            sm = _SCOPE_RE.search(text)
            if sm:
                variants = sorted(v.strip() for v in sm.group(1).split(",") if v.strip())
                text = text[: sm.start()].rstrip()
            blocks.append({"t": "h", "level": level, "text": text, "variants": variants})
            i += 1
            continue
        if stripped.startswith("|"):
            raise LintError(f"body line {i + 1}: tables are not supported; use a definition list")
        if line.startswith((" ", "\t")) and not blocks:
            raise LintError(f"body line {i + 1}: indented text outside a list")
        if line.startswith("> ") or stripped == ">":
            buf: list[str] = []
            while i < n and (lines[i].startswith("> ") or lines[i].strip() == ">"):
                buf.append(lines[i][2:] if lines[i].startswith("> ") else "")
                i += 1
            blocks.append({"t": "note", "text": " ".join(s.strip() for s in buf if s.strip())})
            continue
        if _BULLET_RE.match(line) or _ORDERED_RE.match(line):
            ordered = bool(_ORDERED_RE.match(line))
            items: list[str] = []
            while i < n:
                cur = lines[i]
                bm = _ORDERED_RE.match(cur) if ordered else _BULLET_RE.match(cur)
                if bm:
                    items.append((bm.group(2) if ordered else bm.group(1)).strip())
                    i += 1
                    continue
                if cur.startswith("  ") and cur.strip() and items:
                    if _BULLET_RE.match(cur.strip()) or _ORDERED_RE.match(cur.strip()):
                        raise LintError(f"body line {i + 1}: nested lists are not supported")
                    items[-1] += " " + cur.strip()
                    i += 1
                    continue
                break
            blocks.append({"t": "ol" if ordered else "ul", "items": items})
            continue
        if i + 1 < n and lines[i + 1].startswith(": "):
            items2: list[tuple[str, str]] = []
            while i < n and lines[i].strip() and i + 1 < n and lines[i + 1].startswith(": "):
                term = lines[i].strip()
                i += 1
                defs = [lines[i][2:].strip()]
                i += 1
                while i < n and lines[i].startswith("  ") and lines[i].strip():
                    defs.append(lines[i].strip())
                    i += 1
                items2.append((term, " ".join(defs)))
                while (
                    i < n and not lines[i].strip() and i + 2 < n and lines[i + 2].startswith(": ")
                ):
                    i += 1
            blocks.append({"t": "dl", "items": items2})
            continue
        if line.startswith(": "):
            raise LintError(f"body line {i + 1}: a `: definition` with no term above it")
        buf2: list[str] = []
        while i < n and lines[i].strip():
            cur = lines[i]
            if (
                _HEADING_RE.match(cur)
                or cur.strip().startswith("```")
                or cur.startswith("> ")
                or _BULLET_RE.match(cur)
                or _ORDERED_RE.match(cur)
            ):
                break
            if i + 1 < n and lines[i + 1].startswith(": ") and buf2:
                break
            if cur.strip().startswith("|"):
                raise LintError(
                    f"body line {i + 1}: tables are not supported; use a definition list"
                )
            buf2.append(cur.strip())
            i += 1
        if buf2:
            blocks.append({"t": "p", "text": " ".join(buf2)})
        else:  # pragma: no cover - defensive, a line nothing claimed
            raise LintError(f"body line {i + 1}: cannot parse {line!r}")
    return blocks


def scope_blocks(blocks: list[dict]) -> list[dict]:
    """Attach the effective variant scope to every block. Nested scopes are refused."""
    scoped: list[dict] = []
    active: list[str] = []
    active_level = 0
    for b in blocks:
        if b["t"] == "h":
            if active and b["level"] <= active_level:
                active, active_level = [], 0
            if b["variants"]:
                if active:
                    raise LintError(f"heading {b['text']!r}: a variant scope inside another scope")
                active, active_level = b["variants"], b["level"]
        nb = dict(b)
        nb["scope"] = list(active)
        scoped.append(nb)
    return scoped


def keep_scope(scope: list[str], allowed: list[str] | None) -> list[str] | None:
    """None = drop. [] = unscoped. Otherwise the scope narrowed to `allowed`."""
    if not scope:
        return []
    if allowed is None:
        return list(scope)
    kept = [v for v in scope if v in allowed]
    return kept or None


# ── inline ─────────────────────────────────────────────────────────────────────────

_INLINE_RE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)|\*([^*\s][^*]*)\*")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"`", "", text.lower())).strip("-")
    return s or "section"


def inline_html(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(html.escape(text[pos : m.start()], quote=False))
        if m.group(1) is not None:
            out.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        elif m.group(2) is not None:
            out.append(f"<strong>{html.escape(m.group(2), quote=False)}</strong>")
        elif m.group(5) is not None:
            out.append(f"<em>{html.escape(m.group(5), quote=False)}</em>")
        else:
            href = m.group(4)
            if not re.match(r"^(https://|#|/)", href):
                href = "#"
            out.append(f'<a href="{html.escape(href)}">{html.escape(m.group(3), quote=False)}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def roff_escape(text: str) -> str:
    return text.replace("\\", "\\e")


def roff_line(text: str) -> str:
    """Escape a text line so troff never reads it as a request."""
    if text.startswith((".", "'")):
        return "\\&" + text
    return text


def inline_roff(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(roff_escape(text[pos : m.start()]))
        if m.group(1) is not None:
            out.append("\\fB" + roff_escape(m.group(1)).replace("-", "\\-") + "\\fR")
        elif m.group(2) is not None:
            out.append("\\fB" + roff_escape(m.group(2)) + "\\fR")
        elif m.group(5) is not None:
            out.append("\\fI" + roff_escape(m.group(5)) + "\\fR")
        else:
            out.append(roff_escape(m.group(3)) + " <" + roff_escape(m.group(4)) + ">")
        pos = m.end()
    out.append(roff_escape(text[pos:]))
    return "".join(out)


# ── documents ──────────────────────────────────────────────────────────────────────


class Doc:
    """One parsed source: a guide chapter or a man page."""

    def __init__(self, rel: str, meta: dict, blocks: list[dict], kind: str):
        self.rel = rel
        self.meta = meta
        self.blocks = blocks
        self.kind = kind  # "chapter" | "man"

    @property
    def applies_to(self) -> list[str]:
        v = self.meta.get("applies_to") or []
        return list(v) if isinstance(v, list) else [v]

    @property
    def key(self) -> str:
        if self.kind == "man":
            return f"{self.meta['name']}.{self.meta['section']}"
        return str(self.meta["id"])

    def sections(self) -> list[str]:
        return [b["text"].upper() for b in self.blocks if b["t"] == "h" and b["level"] == 2]

    def section_blocks(self, title: str) -> list[dict]:
        out: list[dict] = []
        inside = False
        for b in self.blocks:
            if b["t"] == "h" and b["level"] == 2:
                inside = b["text"].upper() == title.upper()
                continue
            if inside:
                out.append(b)
        return out

    def documented_verbs(self) -> list[str]:
        """The first word of every COMMANDS term, backticks stripped, from ALL variants."""
        verbs: set[str] = set()
        for b in self.section_blocks("COMMANDS"):
            if b["t"] == "dl":
                for term, _ in b["items"]:
                    word = term.replace("`", "").replace("*", "").strip().split()
                    if word:
                        verbs.add(word[0])
        return sorted(verbs)

    def files(self) -> list[str]:
        paths: list[str] = []
        for b in self.section_blocks("FILES"):
            if b["t"] == "dl":
                for term, _ in b["items"]:
                    t = term.replace("`", "").strip().split()[0]
                    paths.append(t)
        return paths


def load_doc(path: Path, rel: str, kind: str) -> Doc:
    text = path.read_text(encoding="utf-8")
    meta, body = split_front_matter(text)
    blocks = scope_blocks(parse_blocks(body))
    return Doc(rel, meta, blocks, kind)


class Manifest:
    def __init__(self, src: Path):
        self.src = src
        mpath = src / "manifest.yaml"
        if not mpath.is_file():
            raise DeadError(f"manifest not found: {mpath}")
        try:
            data = parse_yaml_lite(mpath.read_text(encoding="utf-8"))
        except LintError as e:
            raise DeadError(f"manifest.yaml does not parse: {e}") from e
        self.raw = data
        self.variants: list[str] = list(data.get("variants") or [])
        self.public: list[str] = list(data.get("public") or [])
        self.out_of_scope: dict = dict(data.get("out_of_scope") or {})
        self.chapter_paths: list[str] = list(data.get("chapters") or [])
        self.man_paths: list[str] = list(data.get("man") or [])
        if not self.variants or not (self.chapter_paths or self.man_paths):
            raise DeadError("manifest.yaml lists no variants or no sources")

    def load(self) -> tuple[list[Doc], list[str]]:
        docs: list[Doc] = []
        errors: list[str] = []
        for kind, rels in (("chapter", self.chapter_paths), ("man", self.man_paths)):
            for rel in rels:
                p = self.src / rel
                if not p.is_file():
                    errors.append(f"{rel}: listed in manifest.yaml but missing")
                    continue
                try:
                    docs.append(load_doc(p, rel, kind))
                except LintError as e:
                    errors.append(f"{rel}: {e}")
        return docs, errors

    def expand(self, applies_to: list[str]) -> list[str]:
        if "*" in applies_to:
            return list(self.variants)
        return [v for v in self.variants if v in applies_to]


def lint_docs(man: Manifest, docs: list[Doc]) -> list[str]:
    errs: list[str] = []
    known = set(man.variants)
    seen: set[str] = set()
    for pv in man.public:
        if pv not in known:
            errs.append(f"manifest.yaml: public variant {pv!r} is not in variants")
    for d in docs:
        keys = MAN_KEYS if d.kind == "man" else CHAPTER_KEYS
        for k in keys:
            if d.meta.get(k) in (None, "", []):
                errs.append(f"{d.rel}: front matter is missing `{k}`")
        if any(d.meta.get(k) in (None, "", []) for k in keys):
            continue
        if d.key in seen:
            errs.append(f"{d.rel}: duplicate id {d.key}")
        seen.add(d.key)
        for v in d.applies_to:
            if v != "*" and v not in known:
                errs.append(f"{d.rel}: applies_to names unknown variant {v!r}")
        for b in d.blocks:
            for v in b.get("variants") or []:
                if v not in known:
                    errs.append(f"{d.rel}: heading {b['text']!r} scopes unknown variant {v!r}")
        if d.kind == "man":
            if d.meta.get("status") not in STATUSES:
                errs.append(f"{d.rel}: status must be one of {STATUSES}")
            have = d.sections()
            for sec in MAN_REQUIRED:
                if sec not in have:
                    errs.append(f"{d.rel}: man page has no `## {sec}` section")
            if "NAME" in have:
                errs.append(f"{d.rel}: NAME is generated from the front matter; remove `## NAME`")
    return errs


# ── renderers ──────────────────────────────────────────────────────────────────────


def _visible(doc: Doc, allowed: list[str] | None):
    for b in doc.blocks:
        scope = keep_scope(b["scope"], allowed)
        if scope is None:
            continue
        yield b, scope


def render_roff(doc: Doc, allowed: list[str] | None) -> str:
    name = str(doc.meta["name"])
    sec = str(doc.meta["section"])
    out = [
        f'.TH "{name.upper()}" "{sec}" "" "awnix" "awnix administration"',
        ".SH NAME",
        roff_line(f"{roff_escape(name)} \\- {inline_roff(str(doc.meta['summary']))}"),
    ]
    if doc.meta.get("status") == "pending-cli":
        out += [
            ".PP",
            "\\fBNot yet available in this release.\\fR This page documents the "
            "interface the command will provide.",
        ]
    for b, _scope in _visible(doc, allowed):
        t = b["t"]
        if t == "h":
            text = b["text"].replace("`", "")
            if b["level"] == 2:
                out.append(f'.SH "{roff_escape(text.upper())}"')
            else:
                out.append(f'.SS "{roff_escape(text)}"')
        elif t == "p":
            out += [".PP", roff_line(inline_roff(b["text"]))]
        elif t == "note":
            out += [".PP", roff_line("\\fBNote:\\fR " + inline_roff(b["text"]))]
        elif t == "code":
            out += [".PP", ".RS 4", ".nf"]
            out += [roff_line(roff_escape(ln)) for ln in b["text"].split("\n")]
            out += [".fi", ".RE"]
        elif t == "dl":
            for term, defn in b["items"]:
                out += [
                    ".TP",
                    roff_line(
                        "\\fB" + inline_roff(term.replace("`", "")).replace("-", "\\-") + "\\fR"
                    ),
                    roff_line(inline_roff(defn)),
                ]
        elif t in ("ul", "ol"):
            for idx, item in enumerate(b["items"], 1):
                mark = "\\(bu" if t == "ul" else f"{idx}."
                out += [f".IP {mark} 4", roff_line(inline_roff(item))]
    return "\n".join(out) + "\n"


def render_html_body(doc: Doc, allowed: list[str] | None) -> tuple[str, list[dict]]:
    parts: list[str] = []
    headings: list[dict] = []
    open_scope: list[str] | None = None
    used: dict[str, int] = {}

    def close():
        nonlocal open_scope
        if open_scope is not None:
            parts.append("</section>")
            open_scope = None

    if doc.kind == "man" and doc.meta.get("status") == "pending-cli":
        parts.append(
            '<p class="awman-pending"><strong>Not yet available in this release.</strong> '
            "This page documents the interface the command will provide.</p>"
        )
    for b, scope in _visible(doc, allowed):
        if (open_scope or []) != scope:
            close()
            if scope:
                parts.append(f'<section data-variants="{html.escape(" ".join(scope))}">')
                open_scope = scope
        if b["t"] == "h":
            hid = slug(b["text"])
            if hid in used:
                used[hid] += 1
                hid = f"{hid}-{used[hid]}"
            else:
                used[hid] = 1
            tag = f"h{b['level']}"
            parts.append(f'<{tag} id="{hid}">{inline_html(b["text"])}</{tag}>')
            headings.append({"id": hid, "text": b["text"], "level": b["level"], "variants": scope})
            continue
        t = b["t"]
        if t == "p":
            parts.append(f"<p>{inline_html(b['text'])}</p>")
        elif t == "note":
            parts.append(f'<aside class="awman-note"><p>{inline_html(b["text"])}</p></aside>')
        elif t == "code":
            parts.append(f"<pre><code>{html.escape(b['text'], quote=False)}</code></pre>")
        elif t == "dl":
            parts.append(
                "<dl>"
                + "".join(
                    f"<dt>{inline_html(term)}</dt><dd>{inline_html(defn)}</dd>"
                    for term, defn in b["items"]
                )
                + "</dl>"
            )
        elif t in ("ul", "ol"):
            parts.append(
                f"<{t}>" + "".join(f"<li>{inline_html(it)}</li>" for it in b["items"]) + f"</{t}>"
            )
    close()
    return "\n".join(parts), headings


_PAGE_CSS = (
    "body{font:16px/1.55 system-ui,sans-serif;max-width:52rem;margin:0 auto;padding:1rem;"
    "color:#1b2330;background:#fff}pre{background:#f2f4f7;padding:.75rem;overflow-x:auto}"
    "code{font-family:ui-monospace,monospace}dt{font-weight:600;margin-top:.5rem}"
    ".awman-note,.awman-pending{border-left:3px solid #c77d00;"
    "padding:.25rem .75rem;background:#fff8e6}"
    "@media (prefers-color-scheme:dark){body{color:#e6e9ef;background:#0a1628}"
    "pre{background:#1a2a40}.awman-note,.awman-pending{background:#2a2410}}"
)


def html_page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_PAGE_CSS}</style></head>\n"
        f'<body>\n<p><a href="index.html">awnix administration</a></p>\n'
        f"<h1>{html.escape(title)}</h1>\n{body}\n</body></html>\n"
    )


def select(man: Manifest, docs: list[Doc], variant: str | None, public: bool):
    """-> (allowed variants or None for all, the docs that survive)."""
    if variant and public:
        raise LintError("--variant and --public are exclusive")
    if variant:
        if variant not in man.variants:
            raise LintError(f"unknown variant {variant!r}; known: {', '.join(man.variants)}")
        allowed: list[str] | None = [variant]
    elif public:
        allowed = list(man.public)
    else:
        allowed = None
    kept = []
    for d in docs:
        applies = man.expand(d.applies_to)
        if allowed is not None and not [v for v in applies if v in allowed]:
            continue
        kept.append(d)
    return allowed, kept


def source_sha(man: Manifest) -> str:
    h = hashlib.sha256()
    for rel in ["manifest.yaml"] + sorted(man.chapter_paths + man.man_paths):
        p = man.src / rel
        if p.is_file():
            h.update(rel.encode())
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def build_guide(
    man: Manifest, docs: list[Doc], variant: str | None = None, public: bool = False
) -> dict:
    allowed, kept = select(man, docs, variant, public)
    variants = list(allowed) if allowed is not None else list(man.variants)
    chapters = []
    pages = []
    for d in kept:
        applies = man.expand(d.applies_to)
        if allowed is not None:
            applies = [v for v in applies if v in allowed]
        body, heads = render_html_body(d, allowed)
        if d.kind == "chapter":
            chapters.append(
                {
                    "id": d.key,
                    "title": str(d.meta["title"]),
                    "applies_to": applies,
                    "html": body,
                    "headings": heads,
                }
            )
        else:
            pages.append(
                {
                    "name": str(d.meta["name"]),
                    "section": str(d.meta["section"]),
                    "summary": str(d.meta["summary"]),
                    "applies_to": applies,
                    "status": str(d.meta["status"]),
                    "html": body,
                    "verbs": d.documented_verbs(),
                }
            )
    chapters.sort(key=lambda c: c["id"])
    pages.sort(key=lambda p: (p["name"], p["section"]))
    return {
        "version": SCHEMA_VERSION,
        "source_sha256": source_sha(man),
        "public": bool(public),
        "variants": variants,
        "chapters": chapters,
        "man": pages,
    }


def dump_guide(guide: dict) -> str:
    return json.dumps(guide, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def render(
    src: Path,
    roff_out: Path | None,
    html_out: Path | None,
    json_out: Path | None,
    variant: str | None = None,
    public: bool = False,
) -> dict:
    man = Manifest(src)
    docs, errors = man.load()
    errors += lint_docs(man, docs)
    if errors:
        raise LintError("; ".join(errors))
    allowed, kept = select(man, docs, variant, public)
    guide = build_guide(man, docs, variant, public)
    if roff_out is not None:
        for d in kept:
            if d.kind != "man":
                continue
            sec = str(d.meta["section"])
            dest = roff_out / f"man{sec}" / f"{d.meta['name']}.{sec}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(render_roff(d, allowed), encoding="utf-8", newline="\n")
    if html_out is not None:
        html_out.mkdir(parents=True, exist_ok=True)
        links = []
        for c in guide["chapters"]:
            (html_out / f"{c['id']}.html").write_text(
                html_page(c["title"], c["html"]), encoding="utf-8", newline="\n"
            )
            links.append(f'<li><a href="{c["id"]}.html">{html.escape(c["title"])}</a></li>')
        mlinks = []
        for p in guide["man"]:
            fn = f"{p['name']}.{p['section']}.html"
            (html_out / fn).write_text(
                html_page(f"{p['name']}({p['section']})", p["html"]), encoding="utf-8", newline="\n"
            )
            mlinks.append(
                f'<li><a href="{fn}">{html.escape(p["name"])}({p["section"]})</a> '
                f"&mdash; {html.escape(p['summary'])}</li>"
            )
        index = (
            "<h2>Guide</h2><ul>"
            + "".join(links)
            + "</ul><h2>Manual pages</h2><ul>"
            + "".join(mlinks)
            + "</ul><p>On the box: <code>man awnix</code>.</p>"
        )
        (html_out / "index.html").write_text(
            html_page("awnix administration", index), encoding="utf-8", newline="\n"
        )
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(dump_guide(guide), encoding="utf-8", newline="\n")
    return guide


def lint(src: Path) -> list[str]:
    man = Manifest(src)
    docs, errors = man.load()
    return errors + lint_docs(man, docs)


# ── self-test ──────────────────────────────────────────────────────────────────────

_FIX_MANIFEST = """version: 1
variants: [awnix, garg-appliance]
public: [awnix]
chapters:
  - guide/01-a.md
man:
  - man/tool.8.md
"""
_FIX_CHAPTER = """---
id: 01-a
title: Install
applies_to: ["*"]
---
## Install it

Run this:

```
.dangerous line \\ with a backslash
```

## On the garg appliance {variants: garg-appliance}

Garg-only text lives here.

## Afterwards

Everyone reads this.
"""
_FIX_MAN = """---
name: tool
section: 8
summary: a tool
applies_to: ["*"]
status: live
---
## SYNOPSIS

`tool` {check|apply}

## DESCRIPTION

.starts with a dot

## COMMANDS

`check`
: look.

`apply [--now]`
: do it.

## FILES

`/etc/tool.conf`
: the config.

## SEE ALSO

awnix(7)
"""


def _write_fixture(root: Path, chapter: str = _FIX_CHAPTER, manpage: str = _FIX_MAN) -> Path:
    (root / "guide").mkdir(parents=True, exist_ok=True)
    (root / "man").mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(_FIX_MANIFEST, encoding="utf-8")
    (root / "guide" / "01-a.md").write_text(chapter, encoding="utf-8")
    (root / "man" / "tool.8.md").write_text(manpage, encoding="utf-8")
    return root


def self_test() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("ok    " if cond else "FAIL  ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        root = _write_fixture(Path(td) / "src")
        out = Path(td) / "out"
        g = render(root, out / "man", out / "html", out / "guide.json")
        roff = (out / "man" / "man8" / "tool.8").read_text(encoding="utf-8")
        check("roff starts with .TH", roff.startswith(".TH "))
        check("a leading dot is escaped", "\\&.starts with a dot" in roff)
        check("a definition list becomes .TP", ".TP\n\\fBcheck\\fR" in roff)
        check("NAME is generated", ".SH NAME\ntool \\- a tool" in roff)
        chap = g["chapters"][0]["html"]
        check("a scoped section is wrapped", '<section data-variants="garg-appliance">' in chap)
        check(
            "documented verbs come from COMMANDS terms", g["man"][0]["verbs"] == ["apply", "check"]
        )
        first = (out / "guide.json").read_bytes()
        render(root, None, None, out / "guide.json")
        check("two renders are byte-identical", first == (out / "guide.json").read_bytes())
        pub = build_guide(Manifest(root), Manifest(root).load()[0], public=True)
        check("--public drops private-variant sections", "Garg-only" not in json.dumps(pub))
        gv = build_guide(Manifest(root), Manifest(root).load()[0], variant="garg-appliance")
        check("--variant keeps that variant's sections", "Garg-only" in json.dumps(gv))
        check("unscoped text survives --public", "Everyone reads this" in json.dumps(pub))
        bad = _write_fixture(Path(td) / "bad", chapter=_FIX_CHAPTER + "\n| a | b |\n")
        check("a table is a lint error", any("tables" in e for e in lint(bad)))
        bad2 = _write_fixture(Path(td) / "bad2", manpage=_FIX_MAN.replace("## FILES", "## FILEZ"))
        check("a man page without FILES is a lint error", any("FILES" in e for e in lint(bad2)))
        bad3 = _write_fixture(
            Path(td) / "bad3",
            chapter=_FIX_CHAPTER.replace("{variants: garg-appliance}", "{variants: nope}"),
        )
        check("an unknown variant scope is a lint error", any("nope" in e for e in lint(bad3)))
    fm = parse_yaml_lite('a: 1\nb: [x, "y"]\nc:\n  - p\n  - q\nd:\n  k: v\n')
    check(
        "front matter parses without yaml",
        fm == {"a": "1", "b": ["x", "y"], "c": ["p", "q"], "d": {"k": "v"}},
    )
    check("a backslash is escaped in roff", roff_escape("a\\b") == "a\\eb")
    if fails:
        print(f"awman self-test FAILED: {len(fails)} rule(s)")
        return 1
    print("awman self-test OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awman", description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("render")
    r.add_argument("--src", type=Path, default=HERE)
    r.add_argument("--roff", type=Path)
    r.add_argument("--html", type=Path)
    r.add_argument("--json", type=Path)
    r.add_argument("--variant")
    r.add_argument("--public", action="store_true")
    li = sub.add_parser("lint")
    li.add_argument("--src", type=Path, default=HERE)
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        if args.cmd == "render":
            if not (args.roff or args.html or args.json):
                print("awman render: give at least one of --roff/--html/--json", file=sys.stderr)
                return 2
            g = render(args.src, args.roff, args.html, args.json, args.variant, args.public)
            print(
                f"awman: {len(g['chapters'])} chapters, {len(g['man'])} man pages, "
                f"variants={','.join(g['variants'])}"
            )
            return 0
        if args.cmd == "lint":
            errs = lint(args.src)
            for e in errs:
                print(f"LINT  {e}")
            print(f"awman lint: {len(errs)} problem(s)")
            return 1 if errs else 0
    except DeadError as e:
        print(f"awman: cannot read sources: {e}", file=sys.stderr)
        return 2
    except LintError as e:
        print(f"awman: {e}", file=sys.stderr)
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
