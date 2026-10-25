"""The catalog schemas beside the MCP manifest schema, and their findings as
sentences. (Not the database schema: that is ``storage/schema.py``.)

``mcps/agent-template.schema.json`` holds an agent template as one
document, its folder: each file the schema's ``x-otodock-documents`` names,
parsed, under its path inside the folder, and every other file at the
folder's top level as ``true`` (``template_document``). ``mcps/skill.schema.json``
holds the front matter of a skill's SKILL.md, the mapping
``services/mcp/skill_format.load_frontmatter`` reads. The platform's
template loader, the skill installers, the release gate's catalog lint and
the catalogs' CI read the same two files, so a rule changes in one place.

A finding reads ``<file>: <field>: <clause>``: the failing rule's
``x-otodock-hint`` (``{value}`` replaced by the value found) for every kind
of rule but a type and a required key, which have words of their own.
jsonschema's message, which repeats the schema, is never shown.

Standard library only, so the gate's lint loads this file by path;
``jsonschema`` (a requirement of the proxy) is imported where a document is
checked. The schemas are code, found beside it: the repository's ``mcps/``
(the image's ``/opt/otodock/mcps``), never the install's MCP folder a test
or a deployment may move.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

TEMPLATE_SCHEMA = "agent-template.schema.json"
SKILL_SCHEMA = "skill.schema.json"
# Every finding at once, up to this many: the author fixes them in one go.
MAX_PROBLEMS = 20
# The cap a release holds a file to; a manifest past it is no manifest.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# The validator kinds with words of their own; every other kind's words are
# the rule's hint.
_REQUIRED, _TYPE, _ENUM, _CONST = "required", "type", "enum", "const"
_OWN_WORDS = frozenset({_TYPE, _REQUIRED})
_REQUIRED_RE = re.compile(r"^'(.+)' is a required property$")
_TYPE_WORDS = {
    "object": "an object", "array": "a list", "string": "a string", "integer": "a whole number",
    "number": "a number", "boolean": "true or false", "null": "null",
}

Reader = Callable[[Path, str], bytes]
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "mcps"


def schema_path(name: str) -> Path:
    return SCHEMA_DIR / name


def load_schema(name: str, path: Path | None = None) -> dict[str, Any]:
    """The schema ``name`` as the platform ships it (``path`` names another
    copy). Read again only when the file changes."""
    p = path or schema_path(name)
    return _load(str(p), p.stat().st_mtime_ns)


@lru_cache(maxsize=8)
def _load(path: str, _mtime_ns: int) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON schema object")
    return data


# --- the template's folder as one document --------------------------------------

def read_plain(folder: Path, rel: str) -> bytes:
    """A document of ``folder``, never through a link and never past the
    cap (the lint's reader; the platform passes its own)."""
    path = folder / rel
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise OSError(f"{rel} is not a regular file")
    with path.open("rb") as fh:
        data = fh.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise OSError(f"{rel} is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB")
    return data


def _entries(folder: Path) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(folder) as it:
            yield from sorted(it, key=lambda e: e.name)
    except OSError:
        return


def _expand(folder: Path, parts: list[str], prefix: str = "") -> Iterator[str]:
    """The paths under ``folder`` one ``x-otodock-documents`` pattern names:
    each segment matched on its own, a dot-name never, a link never entered."""
    head, rest = parts[0], parts[1:]
    for entry in _entries(folder):
        if entry.name.startswith(".") or not fnmatch.fnmatchcase(entry.name, head):
            continue
        rel = f"{prefix}{entry.name}"
        if rest:
            if entry.is_dir(follow_symlinks=False):
                yield from _expand(Path(entry.path), rest, rel + "/")
        elif not entry.is_dir(follow_symlinks=False):
            yield rel


def template_document(folder: Path, schema: dict[str, Any],
                      read: Reader = read_plain) -> tuple[dict[str, Any], list[str]]:
    """``folder`` as the template schema's document, and what could not be
    read into it: a document ``read`` refuses (a link, not a regular file,
    past the cap) or that is not JSON. ``read(folder, rel)`` returns the bytes
    or raises ``OSError``."""
    document: dict[str, Any] = {}
    problems: list[str] = []
    named: set[str] = set()
    for pattern in schema.get("x-otodock-documents") or []:
        for rel in _expand(folder, str(pattern).split("/")):
            named.add(rel)
            try:
                data = read(folder, rel)
            except OSError:
                problems.append(f"{rel} cannot be read: it must be a regular file (not a link) "
                                f"of at most {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB")
                continue
            try:
                document[rel] = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                problems.append(f"Invalid JSON in {rel}: {exc}")
    for entry in _entries(folder):
        if not entry.name.startswith(".") and entry.name not in named and \
                entry.is_file(follow_symlinks=False):
            document[entry.name] = True
    return document, problems


def template_problems(document: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Where a template's document breaks the template schema, as sentences."""
    return _problems(document, schema, label="")


def skill_problems(front_matter: Any, schema: dict[str, Any], label: str) -> list[str]:
    """Where a skill's front matter breaks the skill schema, as sentences
    that start with ``label`` (``skills/x/SKILL.md front matter``)."""
    return _problems(front_matter, schema, label=label)


# --- sentences --------------------------------------------------------------------

# The JSON words for the values a person wrote (Python's repr says None).
_JSON_WORDS = {None: "null", True: "true", False: "false"}


def _value(v: Any) -> str:
    text = _JSON_WORDS[v] if isinstance(v, bool) or v is None else repr(v)
    return text if len(text) <= 80 else text[:77] + "..."


def _field(parts: list[Any]) -> str:
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += f".{p}" if out else str(p)
    return out


def _where(path: list[Any], label: str) -> str:
    """``tasks.json: tasks[0].schedule`` in a template's document (its first
    key is the file), ``<label>: name`` in a single document."""
    if label:
        field = _field(path)
        return f"{label}: {field}" if field else label
    if not path:
        return ""
    field = _field(path[1:])
    return f"{path[0]}: {field}" if field else str(path[0])


def _missing(message: str) -> str:
    m = _REQUIRED_RE.match(message)
    return m.group(1) if m else message


def _sentence(error: Any, label: str) -> str:
    """One validation error, in words."""
    path = list(error.absolute_path)
    kind = str(error.validator)
    node = error.schema if isinstance(error.schema, dict) else {}
    hint = node.get("x-otodock-hint")
    if kind == _REQUIRED:
        if not label and not path:
            return f"Required file missing: {_missing(str(error.message))}"
        clause = f"missing required field '{_missing(str(error.message))}'"
    elif hint and kind not in _OWN_WORDS:
        clause = str(hint).replace("{value}", _value(error.instance))
    elif kind == _TYPE:
        wanted = error.validator_value if isinstance(error.validator_value, list) else [error.validator_value]
        clause = "must be " + " or ".join(_TYPE_WORDS.get(str(t), str(t)) for t in wanted)
    elif kind == _ENUM:
        clause = ("must be one of " + ", ".join(_value(v) for v in error.validator_value)
                  + f" (got {_value(error.instance)})")
    elif kind == _CONST:
        clause = f"must be {_value(error.validator_value)} (got {_value(error.instance)})"
    else:
        # Every other kind carries a hint (the schema test holds it); this
        # is the fallback for a schema that grew one without.
        clause = f"breaks the {kind} rule"
    where = _where(path, label)
    if not where:
        return clause
    # "tasks.json: tasks[0].scope must be one of ...", "mcps.json must be an object".
    return f"{where} {clause}" if clause.startswith("must ") else f"{where}: {clause}"


def _problems(instance: Any, schema: dict[str, Any], *, label: str) -> list[str]:
    from jsonschema import Draft202012Validator
    errors = sorted(Draft202012Validator(schema).iter_errors(instance),
                    key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)))
    out: list[str] = []
    for e in errors:
        sentence = _sentence(e, label)
        if sentence not in out:
            out.append(sentence)
    if len(out) > MAX_PROBLEMS:
        out = out[:MAX_PROBLEMS] + [f"and {len(out) - MAX_PROBLEMS} more"]
    return out
