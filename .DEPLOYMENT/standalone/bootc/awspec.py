#!/usr/bin/env python3
"""awspec -- resolve a layered spec and render it into drop-ins, env files and lists.

ONE engine for every image. The fleet host, the GargBot appliance and a sovereign
install run this same file; only the layer files they are given differ.

    base -> product -> pack -> site -> posture        (a later layer wins)

Every leaf key carries PROVENANCE (which layer file set it), the resolved document
has a content HASH, and the renderer turns it into files with no literal host, port
or distro of its own -- every value comes from a layer.

    awspec resolve  --from DIR... [--layer F...] [--postures F] [--posture NAME]
    awspec explain  [KEY]  (same inputs)       # who set KEY
    awspec validate (same inputs)               # SPEC001 schema, SPEC003 one value per role
    awspec render   (same inputs) --out DIR [--only NAME] [--check] [--dry-run]
    awspec apply    (same inputs) --out-root DIR [--install] [--reload]
    awspec --self-test

Exit 0 clean, 1 a finding (schema, role conflict, render drift), 2 could not judge
(unreadable layer, PyYAML absent for a .yaml layer).

Canonical source: AitherOS/lib/core/aither_spec.py. Byte-mirrored into the awnix
build context as .DEPLOYMENT/standalone/bootc/awspec.py (check_aither_spec.py SPEC010).
Stdlib + PyYAML, Python 3.10-compatible, brand-neutral: nothing here names a product,
a host or a port. Design: docs/architecture/SOVEREIGN-CONFIG-PLANE.md.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:  # PyYAML is optional: a JSON-only layer set needs nothing but the stdlib.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised on a bare image only
    yaml = None  # type: ignore

SCHEMA_ID = "aitherspec/v1"
#: Merge order. Files of equal rank merge in INPUT order: each --from dir sorted by
#: path, dirs in argument order, then --layer files. Exactly one product layer.
LAYER_RANK = {"base": 0, "product": 1, "pack": 2, "site": 3, "posture": 4}
#: Keys a product may lock; a pack, site or posture layer that sets one is refused.
LOCKABLE = ("tls", "air_gap", "secrets", "posture", "product")
_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z0-9_.\-]+)\}")
_SAFE_ENV_RE = re.compile(r"^[A-Za-z0-9_./:@%+,=\-]*$")
#: SPEC004 -- secrets are never rendered (they are filled from the vault by NAME).
#: A rendered env NAME that reads as a credential, or a VALUE shaped like one
#: (a known token prefix, URL userinfo, a PEM block), refuses the whole render.
_SECRET_NAME_RE = re.compile(
    r"(^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?|"
    r"PRIVATE_KEY|BEARER|AUTH)(_|$)", re.I)
_SECRET_VALUE_RE = re.compile(
    r"(sk-ant-|sk-[A-Za-z0-9]{16,}|ghp_|ghs_|gho_|github_pat_|AKIA[0-9A-Z]{12}|pk_live_|"
    r"sk_live_|rk_live_|xox[bpas]-|aither_sk_|-----BEGIN [A-Z ]*PRIVATE KEY|"
    r"[a-z][a-z0-9+.\-]*://[^/\s@:]+:[^/\s@]+@)")


def _guard_value(name: str, value: str, where: str) -> str:
    """SPEC004 + unit-file safety for one rendered NAME=VALUE pair."""
    if _SECRET_NAME_RE.search(name):
        raise SpecError(f"SPEC004 {where}: {name} names a credential -- secrets are never "
                        f"rendered; provision it from the vault by name")
    if _SECRET_VALUE_RE.search(value):
        raise SpecError(f"SPEC004 {where}: the value of {name} is shaped like a credential "
                        f"(token prefix, URL userinfo or key block) -- never rendered")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SpecError(f"{where}: the value of {name} carries a newline/NUL -- it would "
                        f"inject lines into the rendered unit/env file")
    return value


class SpecError(Exception):
    """A layer set that cannot be resolved (bad file, locked key, unknown node)."""


class CannotJudgeError(Exception):
    """An input could not be read at all -> exit 2, never a clean pass."""


# ─── schema ──────────────────────────────────────────────────────────────────


def default_schema_path() -> Path:
    """The schema beside this file (bootc mirror) or in AitherOS/config/schemas."""
    here = Path(__file__).resolve()
    cands = [here.with_name("awspec.schema.json")]
    if len(here.parents) > 2:
        cands.append(here.parents[2] / "config" / "schemas" / "aitherspec.schema.json")
    for cand in cands:
        if cand.is_file():
            return cand
    raise CannotJudgeError("schema not found beside the engine or in config/schemas")


def load_schema(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or default_schema_path()
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CannotJudgeError(f"schema {p}: {exc}") from exc


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "integer": int, "number": (int, float), "null": type(None)}


def _is_type(v: Any, t: str) -> bool:
    if t in ("integer", "number") and isinstance(v, bool):
        return False
    return isinstance(v, _TYPES[t])


def validate_schema(doc: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """The JSON-Schema subset the spec schema uses. Stdlib only, on purpose:
    the base image must validate with nothing installed but python."""
    errs: List[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(doc, x) for x in types):
            return [f"{path}: expected {'|'.join(types)}, got {type(doc).__name__}"]
    if "const" in schema and doc != schema["const"]:
        errs.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and doc not in schema["enum"]:
        errs.append(f"{path}: {doc!r} not in {schema['enum']}")
    if isinstance(doc, str) and "pattern" in schema and not re.search(schema["pattern"], doc):
        errs.append(f"{path}: {doc!r} does not match {schema['pattern']}")
    if isinstance(doc, (int, float)) and not isinstance(doc, bool) and "minimum" in schema:
        if doc < schema["minimum"]:
            errs.append(f"{path}: {doc} < minimum {schema['minimum']}")
    if isinstance(doc, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in doc:
                errs.append(f"{path}: missing required key {req!r}")
        addl = schema.get("additionalProperties", True)
        for k, v in doc.items():
            if k in props:
                errs.extend(validate_schema(v, props[k], f"{path}.{k}"))
            elif addl is False:
                errs.append(f"{path}: unknown key {k!r}")
            elif isinstance(addl, dict):
                errs.extend(validate_schema(v, addl, f"{path}.{k}"))
    if isinstance(doc, list) and "items" in schema:
        for i, v in enumerate(doc):
            errs.extend(validate_schema(v, schema["items"], f"{path}[{i}]"))
    return errs


# ─── loading ─────────────────────────────────────────────────────────────────


def load_file(path: Path) -> Dict[str, Any]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CannotJudgeError(f"cannot read {p}: {exc}") from exc
    if p.suffix == ".json":
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise SpecError(f"{p}: {exc}") from exc
    else:
        if yaml is None:
            raise CannotJudgeError(f"{p} is YAML and PyYAML is not installed")
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # type: ignore[union-attr]
            raise SpecError(f"{p}: {exc}") from exc
    if not isinstance(doc, dict):
        raise SpecError(f"{p}: a layer must be a mapping")
    return doc


def collect_layers(dirs: Iterable[Path] = (), files: Iterable[Path] = ()) -> List[Tuple[str, Dict]]:
    """Every *.yaml|*.yml|*.json under each dir (recursive, sorted) plus explicit files."""
    out: List[Tuple[str, Dict]] = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            raise CannotJudgeError(f"layer dir {d} does not exist")
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix in (".yaml", ".yml", ".json"):
                out.append((str(f), load_file(f)))
    for f in files:
        out.append((str(f), load_file(Path(f))))
    for origin, doc in out:
        if doc.get("layer") not in LAYER_RANK or doc.get("layer") == "posture":
            raise SpecError(f"{origin}: `layer:` must be one of base|product|pack|site "
                            f"(postures come from --postures), got {doc.get('layer')!r}")
    return out


def parse_lane(role: str, lane: Any) -> Dict[str, Any]:
    """`node:port` or {at: node:port, model, kind, scheme} -> an endpoint entry."""
    if isinstance(lane, str):
        lane = {"at": lane}
    if not isinstance(lane, dict) or "at" not in lane and "url" not in lane:
        raise SpecError(f"posture lane {role!r} needs `at: node:port` or `url:`")
    entry = {k: v for k, v in lane.items() if k in ("at", "url", "model", "kind", "scheme")}
    entry["$replace"] = True
    return entry


def posture_layer(doc: Dict[str, Any], name: str, origin: str) -> Dict[str, Any]:
    postures = doc.get("postures") or {}
    if name not in postures:
        raise SpecError(f"posture {name!r} is not declared in {origin} "
                        f"(declared: {', '.join(postures) or 'none'})")
    lanes = (postures[name] or {}).get("scheduler_lanes") or {}
    return {"layer": "posture", "posture": {"name": name},
            "endpoints": {r: parse_lane(r, v) for r, v in lanes.items()}}


# ─── merge ───────────────────────────────────────────────────────────────────


def _strip_replace(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _strip_replace(x) for k, x in v.items() if k != "$replace"}
    if isinstance(v, list):
        return [_strip_replace(x) for x in v]
    return v


def _record(prov: Dict[str, str], path: Tuple[str, ...], val: Any, origin: str) -> None:
    for key in [k for k in prov if k == ".".join(path) or k.startswith(".".join(path) + ".")]:
        del prov[key]
    if isinstance(val, dict) and val:
        for k, v in val.items():
            _record(prov, path + (str(k),), v, origin)
    else:
        prov[".".join(path)] = origin


def _named_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, dict) and "name" in x for x in v)


def merge(dst: Dict[str, Any], src: Dict[str, Any], prov: Dict[str, str], origin: str,
          path: Tuple[str, ...] = ()) -> None:
    """Deep-merge src into dst. Dicts merge, lists of {name:} merge by name, anything
    else (and any dict carrying `$replace: true`) replaces. Provenance follows."""
    for k, v in src.items():
        if k == "$replace" or (not path and k == "layer"):
            continue
        p = path + (str(k),)
        cur = dst.get(k)
        if isinstance(v, dict) and not v.get("$replace") and isinstance(cur, dict):
            merge(cur, v, prov, origin, p)
        elif _named_list(v) and _named_list(cur):
            by = {x["name"]: x for x in cur}
            for item in v:
                if item["name"] in by and not item.get("$replace"):
                    merge(by[item["name"]], item, prov, origin, p + (str(item["name"]),))
                else:
                    by[item["name"]] = _strip_replace(copy.deepcopy(item))
                    _record(prov, p + (str(item["name"]),), by[item["name"]], origin)
            dst[k] = list(by.values())
        else:
            dst[k] = _strip_replace(copy.deepcopy(v))
            _record(prov, p, dst[k], origin)


# ─── resolve ─────────────────────────────────────────────────────────────────


class Resolved:
    def __init__(self, data: Dict[str, Any], provenance: Dict[str, str],
                 problems: List[Tuple[str, str]]):
        self.data = data
        self.provenance = provenance
        self.problems = problems
        canon = json.dumps(data, sort_keys=True, separators=(",", ":"))
        self.hash = hashlib.sha256(canon.encode()).hexdigest()[:16]

    def get(self, dotted: str) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list):
                hit = [x for x in cur if isinstance(x, dict) and x.get("name") == part]
                if not hit:
                    raise KeyError(dotted)
                cur = hit[0]
            else:
                raise KeyError(dotted)
        return cur


def resolve(layers: List[Tuple[str, Dict[str, Any]]],
            postures: Optional[Tuple[str, Dict[str, Any]]] = None,
            posture_name: Optional[str] = None,
            schema: Optional[Dict[str, Any]] = None) -> Resolved:
    ordered = sorted(layers, key=lambda od: LAYER_RANK[od[1]["layer"]])
    products = [o for o, d in ordered if d["layer"] == "product"]
    if len(products) > 1:
        # A second product layer (e.g. a file in the mutable /etc/aither/spec.d that says
        # `layer: product`) would merge at product rank, AFTER the lock is taken and
        # without the lock check -- re-opening tls/air_gap/posture. One machine, one product.
        raise SpecError(f"{len(products)} product layers ({', '.join(products)}): a spec "
                        f"resolves exactly one product")
    data: Dict[str, Any] = {}
    prov: Dict[str, str] = {}
    problems: List[Tuple[str, str]] = []
    if schema is not None:
        for origin, doc in ordered:
            for e in validate_schema(doc, schema):
                problems.append(("SPEC001", f"{origin}: {e}"))
    locked: set = set()
    for origin, doc in ordered:
        rank = LAYER_RANK[doc["layer"]]
        if rank > LAYER_RANK["product"]:
            hit = sorted(k for k in doc if k in locked)
            if hit:
                raise SpecError(f"{origin}: {doc['layer']} layer sets product-locked "
                                f"key(s) {', '.join(hit)}")
        merge(data, doc, prov, origin)
        if doc["layer"] == "product":
            locked |= set((data.get("product") or {}).get("locked") or []) | {"product"}
            if (data.get("posture") or {}).get("locked"):
                locked.add("posture")
    pst = data.get("posture") or {}
    if pst.get("locked"):
        prov["posture.note"] = "posture locked by the product; model-postures ignored"
    elif postures is not None:
        origin, pdoc = postures
        name = posture_name or pst.get("name") or pdoc.get("active")
        if not name:
            raise SpecError(f"{origin}: no posture named and no `active:`")
        merge(data, posture_layer(pdoc, name, origin), prov, f"{origin}#postures.{name}")
    elif posture_name:
        raise SpecError("--posture given without --postures")
    problems.extend(_resolve_endpoints(data, prov))
    problems.extend(_check_roles(data))
    return Resolved(data, prov, problems)


def _resolve_endpoints(data: Dict[str, Any], prov: Dict[str, str]) -> List[Tuple[str, str]]:
    probs: List[Tuple[str, str]] = []
    nodes = data.get("nodes") or {}
    for role, ep in sorted((data.get("endpoints") or {}).items()):
        if isinstance(ep, str):
            ep = data["endpoints"][role] = {"at": ep}
        if "at" in ep and "url" in ep:
            probs.append(("SPEC003", f"endpoints.{role} has two values (at={ep['at']!r} "
                                     f"and url={ep['url']!r}); a role has exactly one"))
            continue
        if "at" in ep:
            node, _, port = str(ep["at"]).rpartition(":")
            if node not in nodes or not port.isdigit():
                probs.append(("SPEC003", f"endpoints.{role}.at={ep['at']!r}: node {node!r} "
                                         f"is not declared in nodes: (or port is not numeric)"))
                continue
            ep["url"] = f"{ep.get('scheme', 'http')}://{nodes[node]['host']}:{port}"
            prov[f"endpoints.{role}.url"] = (
                f"derived: endpoints.{role}.at ({prov.get(f'endpoints.{role}.at', '?')}) + "
                f"nodes.{node}.host ({prov.get(f'nodes.{node}.host', '?')})")
    return probs


def alias_names(data: Dict[str, Any], role: str, field: str, consumer: str) -> List[str]:
    return list((((data.get("env_aliases") or {}).get(role) or {}).get(field) or {})
                .get(consumer) or [])


def _check_roles(data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """SPEC003: one env name belongs to one role, and nothing sets it a second way."""
    probs: List[Tuple[str, str]] = []
    owner: Dict[Tuple[str, str], str] = {}
    for role, fields in sorted((data.get("env_aliases") or {}).items()):
        for field, consumers in sorted((fields or {}).items()):
            for consumer, names in sorted((consumers or {}).items()):
                for n in names or []:
                    key = (consumer, n)
                    if key in owner and owner[key] != f"{role}.{field}":
                        probs.append(("SPEC003", f"env name {n} ({consumer}) is mapped from "
                                                 f"both {owner[key]} and {role}.{field}"))
                    owner[key] = f"{role}.{field}"
    by_name = {n: o for (_c, n), o in owner.items()}
    for d in (data.get("units") or {}).get("dropins") or []:
        for n in (d.get("environment") or {}):
            if n in by_name:
                probs.append(("SPEC003", f"units.dropins.{d.get('name')} sets {n}, which is "
                                         f"role {by_name[n]} -- a second value for that role; "
                                         f"set the role in endpoints: instead"))
    return probs


# ─── render ──────────────────────────────────────────────────────────────────


def _template(value: Any, r: Resolved) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        return str(value)

    def sub(m: "re.Match[str]") -> str:
        try:
            v = r.get(m.group(1))
        except KeyError:
            raise SpecError(f"template ${{{m.group(1)}}} names nothing in the resolved spec")
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        if isinstance(v, (dict,)):
            raise SpecError(f"template ${{{m.group(1)}}} is a mapping, not a value")
        return _template(v, r)
    return _TEMPLATE_RE.sub(sub, value)


def _quote(v: str) -> str:
    """Env-file value: bare when safe, else double-quoted with \\ " $ ` escaped, so a
    shell that sources the file (garg-firstboot `set -a; .`) expands nothing."""
    if _SAFE_ENV_RE.match(v):
        return v
    for ch in ("\\", '"', "$", "`"):
        v = v.replace(ch, "\\" + ch)
    return '"' + v + '"'


def _unit_env(k: str, v: str) -> str:
    """One systemd `Environment=` line. Unquoted, a value with whitespace splits into
    separate assignments; quote the whole K=V and escape \\ and " (systemd rules)."""
    if _SAFE_ENV_RE.match(v):
        return f"Environment={k}={v}"
    return 'Environment="' + f"{k}={v}".replace("\\", "\\\\").replace('"', '\\"') + '"'


def _role_pairs(r: Resolved, out: Dict[str, Any]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    consumer = out.get("consumer", out["name"])
    for role in out.get("roles") or []:
        ep = (r.data.get("endpoints") or {}).get(role)
        if not ep or "url" not in ep:
            raise SpecError(f"output {out['name']} needs role {role!r}, which resolved to nothing")
        names = alias_names(r.data, role, "url", consumer)
        if not names:
            raise SpecError(f"env_aliases.{role}.url has no names for consumer {consumer!r}")
        pairs += [(n, ep["url"]) for n in names]
        for field in ("model", "kind"):
            if ep.get(field) is not None:
                pairs += [(n, str(ep[field])) for n in alias_names(r.data, role, field, consumer)]
    for k, v in (out.get("values") or {}).items():
        pairs.append((k, _template(v, r)))
    return pairs


def _header(r: Resolved, out_name: str) -> str:
    posture = (r.data.get("posture") or {}).get("name", "-")
    return (f"# RENDERED by awspec: output {out_name}, spec {r.hash}, posture {posture}.\n"
            f"# DO NOT HAND-EDIT -- change a spec layer and re-render (awspec explain).\n")


def render(r: Resolved, only: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """name -> {file, content, install}. Raises SpecError on anything unresolved."""
    wanted = set(only or [])
    outputs = list(r.data.get("outputs") or [])
    for d in (r.data.get("units") or {}).get("dropins") or []:
        outputs.append({"kind": "dropin", **d})
    seen = set()
    result: Dict[str, Dict[str, Any]] = {}
    for out in outputs:
        name = out["name"]
        if wanted and name not in wanted:
            continue
        seen.add(name)
        kind = out.get("kind")
        if kind == "dropin":
            section = out.get("section") or ("Container" if str(out.get("unit", "")).endswith(
                ".container") else "Service")
            pairs = _role_pairs(r, out) + [(k, _template(v, r)) for k, v in
                                           (out.get("environment") or {}).items()]
            body = [f"[{section}]"] + [_unit_env(k, _guard_value(k, v, f"output {name}"))
                                       for k, v in pairs]
        elif kind == "env":
            body = [f"{k}={_quote(_guard_value(k, v, f'output {name}'))}"
                    for k, v in _role_pairs(r, out)]
        elif kind == "list":
            try:
                items = r.get(out["from"])
            except KeyError:
                raise SpecError(f"output {name}: from={out.get('from')!r} names nothing")
            if not isinstance(items, list):
                raise SpecError(f"output {name}: {out['from']} is not a list")
            body = [_guard_value(f"{out['from']}[{i}]", _template(x, r), f"output {name}")
                    for i, x in enumerate(items)]
        else:
            raise SpecError(f"output {name}: unknown kind {kind!r}")
        result[name] = {"file": out.get("file", name), "install": out.get("install"),
                        "content": _header(r, name) + "\n".join(body) + "\n"}
    missing = wanted - seen
    if missing:
        raise SpecError(f"--only names no declared output: {', '.join(sorted(missing))}")
    return result


def write_outputs(rendered: Dict[str, Dict[str, Any]], out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for item in rendered.values():
        p = out_dir / item["file"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(item["content"], encoding="utf-8", newline="\n")
        paths.append(p)
    return paths


def check_outputs(rendered: Dict[str, Dict[str, Any]], out_dir: Path) -> List[str]:
    drift = []
    for name, item in sorted(rendered.items()):
        p = out_dir / item["file"]
        have = p.read_text(encoding="utf-8") if p.is_file() else None
        if have != item["content"]:
            drift.append(f"{name}: {p} " + ("is absent" if have is None else "differs"))
    return drift


def apply(r: Resolved, rendered: Dict[str, Dict[str, Any]], out_root: Path,
          install: bool = False, reload: bool = False,
          install_root: str = "/") -> List[str]:
    """Render into out_root/<hash>/, swap out_root/current to it, and (with install)
    copy each output that declares `install:` when its bytes changed. A daemon-reload
    is requested ONLY when an installed unit file changed, and always --no-block: this
    runs during boot and must never wait on a job that is waiting on it.
    install_root prefixes every `install:` path (a sysroot, or a test tree)."""
    target = out_root / r.hash
    write_outputs(rendered, target)
    (target / "manifest.json").write_text(json.dumps(
        {"hash": r.hash, "outputs": {n: i["file"] for n, i in rendered.items()},
         "provenance": r.provenance}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    cur = out_root / "current"
    tmp = out_root / f".current.{os.getpid()}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    try:
        tmp.symlink_to(r.hash)
        os.replace(tmp, cur)
    except OSError:  # no symlinks (Windows dev box): copy instead, same content
        if cur.is_dir() and not cur.is_symlink():
            shutil.rmtree(cur)
        shutil.copytree(target, cur)
    changed: List[str] = []
    if install:
        for name, item in sorted(rendered.items()):
            dest = item.get("install")
            if not dest:
                continue
            d = Path(install_root) / dest.lstrip("/")
            if d.is_file() and d.read_text(encoding="utf-8") == item["content"]:
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            fd, tmpf = tempfile.mkstemp(dir=str(d.parent), prefix=".awspec.")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(item["content"])
            os.replace(tmpf, d)
            changed.append(str(d))
    if reload and any(c.endswith((".conf", ".container", ".service")) for c in changed):
        try:
            subprocess.run(["systemctl", "--no-block", "daemon-reload"], check=False)
        except OSError as exc:
            print(f"awspec: WARN daemon-reload not requested ({exc}); units re-read on the "
                  f"next reload", file=sys.stderr)
    return changed


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _inputs(a: argparse.Namespace) -> Resolved:
    layers = collect_layers(a.from_dirs or [], a.layer or [])
    if not layers:
        raise CannotJudgeError("no layers given (--from DIR / --layer FILE)")
    postures = None
    if a.postures:
        pp = Path(a.postures)
        if pp.is_file():
            postures = (str(pp), load_file(pp))
        elif a.posture:
            raise CannotJudgeError(f"--postures {pp} does not exist")
        else:
            print(f"awspec: NOTE {pp} absent -- no posture layer", file=sys.stderr)
    schema = load_schema(Path(a.schema) if a.schema else None)
    return resolve(layers, postures, a.posture, schema)


def _report(problems: List[Tuple[str, str]]) -> int:
    for rule, msg in problems:
        print(f"{rule} {msg}")
    return 1 if problems else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awspec", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("resolve", "explain", "validate", "render", "apply"):
        s = sub.add_parser(name)
        s.add_argument("--from", dest="from_dirs", action="append", help="layer directory")
        s.add_argument("--layer", action="append", help="one layer file")
        s.add_argument("--postures", help="model-postures.yaml (the posture layer)")
        s.add_argument("--posture", help="posture name (default: that file's active:)")
        s.add_argument("--schema", help="schema path (default: beside the engine)")
        if name == "explain":
            s.add_argument("key", nargs="?", default="")
        if name in ("render", "apply"):
            s.add_argument("--only", action="append", help="render just this output")
        if name == "render":
            s.add_argument("--out", help="output directory")
            s.add_argument("--check", action="store_true", help="exit 1 if --out differs")
            s.add_argument("--dry-run", action="store_true", help="print, write nothing")
        if name == "apply":
            s.add_argument("--out-root", required=True)
            s.add_argument("--install", action="store_true")
            s.add_argument("--reload", action="store_true")
            s.add_argument("--install-root", default="/",
                           help="prefix for install: paths (default /)")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        r = _inputs(a)
        if a.cmd == "resolve":
            print(json.dumps({"hash": r.hash, "spec": r.data}, indent=1, sort_keys=True))
            return _report(r.problems)
        if a.cmd == "explain":
            for k, v in sorted(r.provenance.items()):
                if k == a.key or k.startswith(a.key):
                    print(f"{k} <- {v}")
            return 0
        if a.cmd == "validate":
            rc = _report(r.problems)
            print(f"awspec: {'FAIL' if rc else 'OK'} spec {r.hash}, {len(r.problems)} problem(s)")
            return rc
        if r.problems:
            return _report(r.problems)
        rendered = render(r, a.only)
        if a.cmd == "render":
            if a.dry_run or not a.out:
                for n, item in sorted(rendered.items()):
                    print(f"==> {item['file']} ({n})\n{item['content']}", end="")
                return 0
            if a.check:
                drift = check_outputs(rendered, Path(a.out))
                for d in drift:
                    print(f"DRIFT {d}")
                return 1 if drift else 0
            for p in write_outputs(rendered, Path(a.out)):
                print(f"wrote {p}")
            return 0
        changed = apply(r, rendered, Path(a.out_root), a.install, a.reload, a.install_root)
        print(f"awspec: applied spec {r.hash}; installed {len(changed)} changed file(s)")
        for c in changed:
            print(f"  installed {c}")
        return 0
    except CannotJudgeError as exc:
        print(f"awspec: CANNOT JUDGE {exc}", file=sys.stderr)
        return 2
    except SpecError as exc:
        print(f"SPEC001 {exc}")
        return 1


def self_test() -> int:
    """Prove each arm can fail: merge, $replace, lock, posture, SPEC003, render."""
    bad: List[str] = []

    def chk(cond: bool, label: str) -> None:
        if not cond:
            bad.append(label)

    def _refused(fn: Callable[[], object]) -> bool:
        """True when ``fn`` raises SpecError -- the refusal these cases assert."""
        try:
            fn()
        except SpecError:
            return True
        return False

    aliases = {"orch": {"url": {"sched": ["A_URL", "B_URL"], "app": ["APP_URL"]},
                        "model": {"sched": ["A_MODEL"]}}}
    base = ("base", {"layer": "base", "env_aliases": aliases, "tls": {"enabled": True}})
    prod = ("prod", {"layer": "product", "product": {"name": "p", "locked": ["tls"]},
                     "nodes": {"svc": {"host": "orch-svc"}},
                     "endpoints": {"orch": {"at": "svc:81", "model": "m0"}},
                     "outputs": [{"name": "d", "kind": "dropin", "consumer": "sched",
                                  "unit": "x.container", "roles": ["orch"]},
                                 {"name": "e", "kind": "env", "consumer": "app",
                                  "roles": ["orch"], "values": {"D": "${site.d}"}},
                                 {"name": "l", "kind": "list", "from": "site.dirs"}]})
    site = ("site", {"layer": "site", "nodes": {"far": {"host": "10.0.0.5"}},
                     "site": {"d": "x", "dirs": ["/a", "/b"]}})
    pdoc = {"active": "p1", "postures": {"p1": {"scheduler_lanes": {"orch": "far:82"}},
                                         "p2": {}}}
    r = resolve([site, prod, base], ("pf", pdoc))
    chk(not r.problems, f"clean spec has problems: {r.problems}")
    chk(r.data["endpoints"]["orch"]["url"] == "http://10.0.0.5:82", "posture lane did not win")
    chk("model" not in r.data["endpoints"]["orch"], "$replace kept the product's model")
    chk(r.provenance.get("endpoints.orch.at", "").startswith("pf#"), "provenance lost")
    out = render(r)
    chk("Environment=A_URL=http://10.0.0.5:82\nEnvironment=B_URL=http://10.0.0.5:82"
        in out["d"]["content"], "drop-in did not render both aliases")
    chk("APP_URL=http://10.0.0.5:82\nD=x" in out["e"]["content"], "env did not render")
    chk(out["l"]["content"].endswith("/a\n/b\n"), "list did not render")
    r2 = resolve([site, prod, base], ("pf", pdoc), "p2")
    chk(r2.data["endpoints"]["orch"]["url"] == "http://orch-svc:81", "posture flip ignored")
    chk(r2.hash != r.hash, "hash did not move with the posture")
    locked = ("s", {"layer": "site", "tls": {"enabled": False}})
    chk(_refused(lambda: resolve([locked, prod, base])),
        "a site layer overrode a product-locked key")
    two = ("s2", {"layer": "site", "endpoints": {"orch": {"url": "http://y:1"}}})
    r3 = resolve([two, prod, base])
    chk(any(p[0] == "SPEC003" for p in r3.problems), "SPEC003 missed at+url on one role")
    raw = ("s3", {"layer": "site", "units": {"dropins": [
        {"name": "x", "unit": "x.container", "environment": {"B_URL": "http://z:9"}}]}})
    r4 = resolve([raw, site, prod, base], ("pf", pdoc))
    chk(any(p[0] == "SPEC003" for p in r4.problems), "SPEC003 missed a raw second value")
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"layer": {"enum": ["base"]}}}
    chk(validate_schema({"layer": "base", "typo": 1}, schema) != [], "schema missed a typo")
    chk(validate_schema({"layer": "base"}, schema) == [], "schema refused a valid doc")
    chk(_refused(lambda: render(r, ["nope"])), "--only accepted an undeclared output")
    prod2 = ("p2", {"layer": "product", "tls": {"enabled": False}})
    chk(_refused(lambda: resolve([prod2, prod, base])),
        "a second product layer re-opened a locked key")
    for leak in ({"API_TOKEN": "x"}, {"D": "https://u:pw@host/x"}, {"D": "gh" + "p_abc"},
                 {"D": "a\nExecStartPre=/bin/true"}):
        lk = ("s4", {"layer": "site", "outputs": [{"name": "e", "values": leak}]})
        layers = [lk, site, prod, base]
        chk(_refused(lambda: render(resolve(layers, ("pf", pdoc)), ["e"])),  # noqa: B023
            f"rendered a secret/injection: {leak}")
    sp = ("s5", {"layer": "site", "units": {"dropins": [
        {"name": "q", "unit": "q.container", "environment": {"Q": "a b"}}]}})
    chk('Environment="Q=a b"' in render(resolve([sp, site, prod, base]), ["q"])["q"]["content"],
        "a drop-in value with a space was not quoted")
    chk(_quote("a $(id)`x`") == '"a \\$(id)\\`x\\`"', "env value not shell-escaped")
    if bad:
        for b in bad:
            print(f"SELF-TEST FAIL: {b}")
        return 1
    print("SELF-TEST: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
