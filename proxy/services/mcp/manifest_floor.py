"""The MCP manifest schema and the platform floor an entry's fields imply.

``mcps/manifest.schema.json`` is the catalog contract for a package's
``manifest.json``. Besides the shapes it carries two annotations the
validators ignore: ``x-otodock-since`` on a field (the platform release that
first reads it) and ``x-otodock-floors`` on ``server.source`` (a value
pattern and its release, such as a PyPI source with extras). An entry that
gives such a field a value needs ``platform_min_version`` at least that
release: an older platform refuses it, drops it or runs it broken.

Standard library only, so the release gate's catalog lint can load this file
by path under the host's ``python3`` (3.10). The catalogs' registry
generator walks the schema the same way; the gate's tests hold the two walks
equal. ``schema_errors`` needs ``jsonschema`` (the proxy's lockfile pins it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_NAME = "manifest.schema.json"
# What a manifest that declares nothing needs: every release reads the base.
BASE_FLOOR = "1.0.0"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def schema_path() -> Path:
    import config
    return config.MCPS_DIR / SCHEMA_NAME


def load_schema(path: Path | None = None) -> dict:
    return json.loads((path or schema_path()).read_text(encoding="utf-8"))


def version_key(version: str) -> tuple[int, int, int]:
    """``1.8.0`` → (1, 8, 0); anything unparseable sorts as 0.0.0."""
    m = _VERSION_RE.match(str(version or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _declared(value) -> bool:
    # A field left null, empty or false asks nothing of the platform.
    return value not in (None, "", [], {}, False)


def _resolve(root: dict, ref: str) -> dict:
    node = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _walk(inst, node, root: dict, path: str, found: dict, seen: set) -> None:
    """Collect the annotations of every subschema that describes ``inst``.
    Composition keywords are followed without evaluating them: an annotation
    sits on a property, and it applies whenever the manifest gives that
    property a value."""
    if not isinstance(node, dict):
        return
    key = (id(node), id(inst))
    if key in seen:
        return
    seen.add(key)
    if isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/"):
        _walk(inst, _resolve(root, node["$ref"]), root, path, found, seen)
    since = node.get("x-otodock-since")
    if since and _declared(inst):
        _note(found, path or "/", since)
    if isinstance(inst, str):
        for floor in node.get("x-otodock-floors") or []:
            if re.search(floor["pattern"], inst):
                _note(found, f"{path} matching {floor['pattern']}", floor["since"])
    for kw in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(kw) or []:
            _walk(inst, sub, root, path, found, seen)
    for kw in ("then", "else"):
        if kw in node:
            _walk(inst, node[kw], root, path, found, seen)
    if isinstance(inst, dict):
        props = node.get("properties") or {}
        extra = node.get("additionalProperties")
        for name, value in inst.items():
            if name in props:
                _walk(value, props[name], root, f"{path}/{name}", found, seen)
            elif isinstance(extra, dict):
                _walk(value, extra, root, f"{path}/{name}", found, seen)
    elif isinstance(inst, list) and isinstance(node.get("items"), dict):
        for i, value in enumerate(inst):
            _walk(value, node["items"], root, f"{path}/{i}", found, seen)


def _note(found: dict, where: str, since: str) -> None:
    if version_key(since) > version_key(found.get(where, "0.0.0")):
        found[where] = since


def platform_floor(manifest: dict, schema: dict | None = None) -> tuple[str, list[str]]:
    """The lowest platform release that reads every field ``manifest``
    uses, and why (``"/result_files (1.8.0)"``). A key the schema does not
    know adds nothing: catalogs are additive."""
    root = schema if schema is not None else load_schema()
    found: dict[str, str] = {}
    _walk(manifest, root, root, "", found, set())
    floor = max(found.values(), key=version_key, default=BASE_FLOOR)
    if version_key(floor) < version_key(BASE_FLOOR):
        floor = BASE_FLOOR
    return floor, [f"{where} ({since})" for where, since in sorted(found.items())]


def declared_floor(manifest: dict) -> str:
    """The ``platform_min_version`` a manifest states (the base when none)."""
    value = manifest.get("platform_min_version")
    return value if isinstance(value, str) and _VERSION_RE.match(value) else BASE_FLOOR


def schema_errors(manifest, schema: dict | None = None) -> list[str]:
    """Where ``manifest`` breaks the schema, one line per error
    (``/server/source: ...``). Raises ImportError without jsonschema."""
    from jsonschema import Draft202012Validator
    root = schema if schema is not None else load_schema()
    errors = sorted(Draft202012Validator(root).iter_errors(manifest), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors:
        where = "/" + "/".join(str(p) for p in e.absolute_path)
        out.append(f"{where}: {e.message[:300]}")
    return out
