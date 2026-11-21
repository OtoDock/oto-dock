"""The check document (CHECKS.md "The check document"): its schema, its
validation, where it lives and how it is read.

An agent's checks live in ``config/checks/<name>/check.json`` (a script
beside it), the one tree only managers write; a user's own in
``users/<u>/checks/<name>/``. The files are the source; the index table
(``agent_checks``) is a cache keyed by the file's sha256, so a hand edit is
picked up at the next evaluation and shown as "updated by file". Writes
through the routes go through here too: the file, the git commit of the
config repo, the satellite fan-out and the index row, in that order.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
from core import agent_folder, layout
from services.checks import patterns
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
from storage.checks import db_checks

logger = logging.getLogger("checks")

CHECK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
CHECKS_DIR_NAME = "checks"
DOC_FILE = "check.json"
APPLIES = ("chats", "tasks", "delegations")
SECTIONS = ("schema", "script", "handler", "judge")
KINDS = ("code", "document", "spreadsheet", "presentation", "image", "video", "audio",
         "data", "text", "any")
EVENTS = ("commit", "push", "build", "test", "render", "publish")
PLACES = ("project", "git")
JUDGE_ON = ("auto", "platform")
SEVERITIES = ("error", "warning", "note")
MAX_ROUNDS = 3
DEFAULT_ROUNDS = 3
MAX_DOC_BYTES = 64 * 1024
_DOC_READ_MAX_BYTES = 4 * MAX_DOC_BYTES
MAX_SCRIPT_BYTES = 256 * 1024
MAX_RUBRIC_CHARS = 16 * 1024
MAX_INPUTS = 16
MAX_GLOBS = 32
MAX_COMMANDS = 16
MAX_MCPS = 8
SCRIPT_TIMEOUT_DEFAULT = 600
SCRIPT_TIMEOUT_MAX = 7200
JUDGE_TIMEOUT_DEFAULT = 600
JUDGE_TIMEOUT_MIN = 30
JUDGE_TIMEOUT_MAX = 1800
MAX_CHECKS_PER_TREE = 64

# The verdict every kind answers with (CHECKS.md "Verdicts").
VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["pass"],
}


class CheckError(ValueError):
    """A document that cannot be accepted; the message is the user's."""


@dataclass
class CheckDoc:
    """One check as loaded: the normalized document plus where it is."""
    agent: str
    owner: str            # "" for the agent's, else the username
    name: str
    doc: dict
    folder: Path
    doc_sha256: str
    script_sha256: str = ""
    updated_by: str = ""
    updated_at: str = ""
    problems: list[str] = field(default_factory=list)
    # The index's last valid document of an agent check that was MANDATORY
    # and whose file is now broken or gone: the evaluator reports it on
    # every turn it applies to (an ``error`` verdict), never skips it.
    held: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"user:{self.name}" if self.owner else f"agent:{self.name}"

    @property
    def mandatory(self) -> bool:
        return bool(self.doc.get("mandatory")) and not self.owner

    @property
    def applies(self) -> list[str]:
        return list(self.doc.get("applies") or APPLIES)

    @property
    def rounds(self) -> int:
        return int(self.doc.get("rounds", DEFAULT_ROUNDS))

    @property
    def sections(self) -> list[str]:
        return [s for s in SECTIONS if self.doc.get(s)]

    def script_path(self) -> Path | None:
        script = self.doc.get("script") or {}
        run = script.get("run") if isinstance(script, dict) else None
        return (self.folder / run) if run else None


# ── validation ──────────────────────────────────────────────────────────────


def _str(doc: dict, key: str, *, max_len: int, required: bool = False, default: str = "") -> str:
    v = doc.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        raise CheckError(f"{key} must be text")
    v = v.strip() if key != "rubric" else v
    if required and not v:
        raise CheckError(f"{key} is required")
    if len(v) > max_len:
        raise CheckError(f"{key} is longer than {max_len} characters")
    return v


def _str_list(doc: dict, key: str, *, allowed: tuple[str, ...] | None, max_items: int,
              default: list | None = None) -> list[str]:
    v = doc.get(key)
    if v is None:
        return list(default or [])
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise CheckError(f"{key} must be a list of names")
    out = list(dict.fromkeys(x.strip() for x in v if x.strip()))
    if len(out) > max_items:
        raise CheckError(f"{key}: at most {max_items} entries")
    if allowed is not None:
        bad = [x for x in out if x not in allowed]
        if bad:
            raise CheckError(f"{key}: unknown {bad[0]!r} (one of {', '.join(allowed)})")
    return out


def _int(doc: dict, key: str, *, lo: int, hi: int, default: int) -> int:
    v = doc.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        raise CheckError(f"{key} must be a whole number")
    if not lo <= v <= hi:
        raise CheckError(f"{key} must be between {lo} and {hi}")
    return v


def _tree_relative(path: str, key: str, owner: str = "") -> str:
    """An input path: under ``workspace/`` or ``knowledge/``, or under
    ``users/<owner>/`` for a person's own check — never another person's
    tree, and never a hidden segment (``.credentials``, the CLI state dirs)."""
    p = (path or "").strip().replace("\\", "/")
    try:
        rel = normalize_rel_path(p) if p and not p.startswith("/") else ""
    except PathOutsideRoot:
        rel = ""
    head = layout.head_of(rel) if rel else ""
    parts = rel.split("/")
    if (not rel or head not in layout.FILE_HEADS
            or any(seg.startswith(".") for seg in parts)
            or (head == layout.USERS and (not owner or len(parts) < 3 or parts[1] != owner))):
        raise CheckError(f"{key}: {path!r} must be a path under workspace/, knowledge/ or users/<you>/")
    return rel


def validate_condition(cond) -> dict:
    if cond is None:
        return {}
    if not isinstance(cond, dict):
        raise CheckError("condition must be an object")
    out: dict = {}
    if cond.get("always") is not None:
        if not isinstance(cond["always"], bool):
            raise CheckError("condition.always must be true or false")
        if cond["always"]:
            out["always"] = True
    kinds = _str_list(cond, "kinds", allowed=KINDS, max_items=len(KINDS))
    if kinds:
        out["kinds"] = kinds
    events = cond.get("events")
    if events is not None:
        if not isinstance(events, list) or any(not isinstance(x, str) for x in events):
            raise CheckError("condition.events must be a list of names")
        clean = []
        for e in events:
            e = e.strip()
            if not e:
                continue
            if e in EVENTS or (e.startswith("tool:") and len(e) > 5 and " " not in e):
                clean.append(e)
            else:
                raise CheckError(f"condition.events: unknown {e!r} (one of {', '.join(EVENTS)}, "
                                 "or tool:<mcp tool name>)")
        if clean:
            out["events"] = list(dict.fromkeys(clean))
    places = _str_list(cond, "places", allowed=PLACES, max_items=len(PLACES))
    if places:
        out["places"] = places
    globs = _str_list(cond, "globs", allowed=None, max_items=MAX_GLOBS)
    if globs:
        for g in globs:
            try:
                patterns.check_glob(g)
            except patterns.PatternError as e:
                raise CheckError(f"condition.globs: {g[:80]!r} is not a glob a check takes ({e})") from e
        out["globs"] = globs
    commands = _str_list(cond, "commands", allowed=None, max_items=MAX_COMMANDS)
    if commands:
        for c in commands:
            try:
                patterns.check(c)
            except patterns.PatternError as e:
                raise CheckError(f"condition.commands: {c[:80]!r} is not a valid pattern ({e}); "
                                 "a check's patterns are RE2 — no backreferences, no lookaround") from e
        out["commands"] = commands
    return out


def validate_check_doc(doc, *, owner: str = "") -> dict:
    """The normalized document, or ``CheckError``. ``owner`` non-empty
    means a user's own check: ``mandatory`` is meaningless there."""
    if not isinstance(doc, dict):
        raise CheckError("a check is an object")
    if len(json.dumps(doc, default=str)) > MAX_DOC_BYTES:
        raise CheckError(f"the document is larger than {MAX_DOC_BYTES // 1024} KB")
    unknown = sorted(set(doc) - {"name", "description", "mandatory", "applies", "condition",
                                 "rounds", "inputs", *SECTIONS})
    if unknown:
        raise CheckError(f"unknown keys {unknown}")
    name = _str(doc, "name", max_len=64, required=True)
    if not CHECK_NAME_RE.match(name):
        raise CheckError("name: lowercase letters, digits, '-' and '_', starting with a letter or digit")
    out: dict = {
        "name": name,
        "description": _str(doc, "description", max_len=500),
        "mandatory": bool(doc.get("mandatory")) if not owner else False,
        "applies": _str_list(doc, "applies", allowed=APPLIES, max_items=3, default=list(APPLIES)) or list(APPLIES),
        "condition": validate_condition(doc.get("condition")),
        "rounds": _int(doc, "rounds", lo=0, hi=MAX_ROUNDS, default=DEFAULT_ROUNDS),
        "inputs": [_tree_relative(p, "inputs", owner) for p in
                   _str_list(doc, "inputs", allowed=None, max_items=MAX_INPUTS)],
    }
    if doc.get("mandatory") is not None and not isinstance(doc.get("mandatory"), bool):
        raise CheckError("mandatory must be true or false")
    schema = doc.get("schema")
    if schema is not None:
        if not isinstance(schema, dict):
            raise CheckError("schema must be a JSON schema object")
        try:
            import jsonschema
            jsonschema.Draft202012Validator.check_schema(schema)
        except Exception as e:  # the library's own message is the user's
            raise CheckError(f"schema: {str(e).splitlines()[0][:200]}") from e
        try:
            patterns.check_schema(schema)
        except patterns.PatternError as e:
            raise CheckError(f"schema: {e}; a check's patterns are RE2 — no backreferences, "
                             "no lookaround") from e
        out["schema"] = schema
    script = doc.get("script")
    if script is not None:
        if not isinstance(script, dict):
            raise CheckError("script must be an object with run and timeout")
        run = _str(script, "run", max_len=80, required=True)
        if not SCRIPT_NAME_RE.match(run) or run == DOC_FILE:
            raise CheckError("script.run: a plain file name in the check's folder")
        out["script"] = {"run": run,
                         "timeout": _int(script, "timeout", lo=1, hi=SCRIPT_TIMEOUT_MAX,
                                         default=SCRIPT_TIMEOUT_DEFAULT)}
    handler = doc.get("handler")
    if handler is not None:
        if not isinstance(handler, dict):
            raise CheckError("handler must be an object with app and handler")
        app = _str(handler, "app", max_len=64, required=True)
        hname = _str(handler, "handler", max_len=64, required=True)
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", app) or not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", hname):
            raise CheckError("handler: the app slug and the handler name are lowercase words")
        out["handler"] = {"app": app, "handler": hname}
    judge = doc.get("judge")
    if judge is not None:
        if not isinstance(judge, dict):
            raise CheckError("judge must be an object with a rubric")
        rubric = _str(judge, "rubric", max_len=MAX_RUBRIC_CHARS, required=True)
        threshold = judge.get("threshold")
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
                raise CheckError("judge.threshold must be a number between 0 and 1")
        judge_on = _str(judge, "judge_on", max_len=16, default="auto") or "auto"
        if judge_on not in JUDGE_ON:
            raise CheckError("judge.judge_on is auto or platform")
        engine = _str(judge, "engine", max_len=32)
        if engine:
            from core.session.session_manager import valid_execution_paths
            known = valid_execution_paths()
            if engine not in known:
                raise CheckError(
                    f"judge.engine is one of: {', '.join(sorted(known))}")
        out["judge"] = {
            "rubric": rubric.strip(),
            "engine": engine,
            "model": _str(judge, "model", max_len=120),
            "threshold": float(threshold) if threshold is not None else None,
            "mcps": _judge_mcps(_str_list(judge, "mcps", allowed=None, max_items=MAX_MCPS)),
            "judge_on": judge_on,
            "timeout": _int(judge, "timeout", lo=JUDGE_TIMEOUT_MIN, hi=JUDGE_TIMEOUT_MAX,
                            default=JUDGE_TIMEOUT_DEFAULT),
        }
    if not any(out.get(s) for s in SECTIONS):
        raise CheckError("a check needs at least one of schema, script, handler or judge")
    return out


# MCPs a judge never gets, whatever the check lists: the judge reads and
# assesses, it does not write the agent's memory or govern checks.
JUDGE_NEVER_MCPS = ("memory-mcp", "checks-mcp")


def _judge_mcps(names: list[str]) -> list[str]:
    for n in names:
        if n in JUDGE_NEVER_MCPS:
            raise CheckError(f"a judge never gets {n}")
    return names


def canonical_json(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_text(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


# ── where checks live ───────────────────────────────────────────────────────


_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _agent_dir(agent: str) -> Path:
    """The agent's directory, contained: a normalized child of ``AGENTS_DIR``
    and nothing else — the routes check the agent exists, this keeps a name
    with a separator or a dot-dot out of every path built here."""
    root = os.path.normpath(str(config.AGENTS_DIR))
    candidate = os.path.normpath(os.path.join(root, str(agent or "")))
    if not candidate.startswith(root + os.sep) or os.path.dirname(candidate) != root:
        raise CheckError("not an agent")
    return Path(candidate)


def checks_root(agent: str, owner: str = "") -> Path:
    """``config/checks`` for the agent's, ``users/<u>/checks`` for a user's."""
    agent_dir = _agent_dir(agent)
    if owner:
        if not _OWNER_RE.match(owner):
            raise CheckError("not a username")
        return layout.user_dir(agent_dir, owner) / CHECKS_DIR_NAME
    return agent_dir / layout.CONFIG / CHECKS_DIR_NAME


def check_folder(agent: str, owner: str, name: str) -> Path:
    if not CHECK_NAME_RE.match(name or ""):
        raise CheckError("not a check name")
    return checks_root(agent, owner) / name


def _beneath(agent: str, path: Path) -> tuple[Path, str]:
    """``path`` (joined from the agent's own folder, never resolved) as the
    agent's real folder and the rel below it (``core/agent_folder.py``: the
    agent's folder may be the operator's link). Every read, write and
    removal of a check opens that rel strictly (``safe_fs``): a session that
    writes /config, or a person's tree, can plant a link at any part of it,
    and no link below the agent's folder is followed."""
    return agent_folder.beneath_agent(path, agent)


_LINKED = "the check's folder is reached through a link"


def _read_folder(agent: str, owner: str, folder: Path) -> CheckDoc | None:
    """One folder → a CheckDoc (with ``problems`` when the file is broken),
    or None when there is no document (missing, a link, not a plain file)."""
    base, rel = _beneath(agent, folder)
    try:
        # The file is written indented: the read is bounded far above the
        # document's own rule, which the validation keeps.
        raw = safe_fs.read_bytes_beneath(base, f"{rel}/{DOC_FILE}", max_size=_DOC_READ_MAX_BYTES)
    except safe_fs.FileTooLarge:
        return CheckDoc(agent=agent, owner=owner, name=folder.name, doc={"name": folder.name}, folder=folder,
                        doc_sha256="", problems=[f"the document is larger than {MAX_DOC_BYTES // 1024} KB"])
    except _NO_SUCH_FILE:
        return None
    except OSError as e:
        # Could not look is not "no check": the row stays (``_sync_index``).
        return CheckDoc(agent=agent, owner=owner, name=folder.name, doc={"name": folder.name}, folder=folder,
                        doc_sha256="", problems=[f"the document cannot be read: {type(e).__name__}"])
    problems: list[str] = []
    doc: dict = {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
        doc = validate_check_doc(parsed, owner=owner)
        if doc["name"] != folder.name:
            problems.append(f"the document's name {doc['name']!r} is not the folder's ({folder.name})")
    except (ValueError, CheckError) as e:
        problems.append(str(e).splitlines()[0][:300] if str(e) else "not valid JSON")
        doc = {"name": folder.name}
    script_sha = ""
    script = doc.get("script") or {}
    if isinstance(script, dict) and script.get("run"):
        try:
            script_sha = sha256_text(_read_script_bytes(agent, folder, script["run"]))
        except FileNotFoundError:
            problems.append(f"the script {script['run']} is missing")
        except safe_fs.FileTooLarge:
            problems.append("the script is larger than 256 KB")
        except (safe_fs.SymlinkRefused, safe_fs.NotRegularFile):
            problems.append(f"the script {script['run']} is missing")
        except OSError as e:
            problems.append(f"the script cannot be read: {type(e).__name__}")
    return CheckDoc(agent=agent, owner=owner, name=folder.name, doc=doc, folder=folder,
                    doc_sha256=sha256_text(raw), script_sha256=script_sha, problems=problems)


# What is no file at a check's name: missing, a link, a folder or a special
# file in its place. Any other error means the file could not be read.
_NO_SUCH_FILE = (FileNotFoundError, NotADirectoryError, safe_fs.SymlinkRefused, safe_fs.NotRegularFile)


def _read_script_bytes(agent: str, folder: Path, run: str) -> bytes:
    base, rel = _beneath(agent, folder)
    return safe_fs.read_bytes_beneath(base, f"{rel}/{run}", max_size=MAX_SCRIPT_BYTES)


def read_script(check: CheckDoc) -> bytes:
    """The check's script as its folder holds it now, read with no link
    followed and under the 256 KB cap (``safe_fs`` errors are ``OSError``)."""
    script = check.doc.get("script") or {}
    run = script.get("run") if isinstance(script, dict) else None
    if not run:
        raise FileNotFoundError("the check has no script")
    return _read_script_bytes(check.agent, check.folder, str(run))


# (agent, owner) → the names whose folder was missing on the last load.
_missing_once: dict[tuple[str, str], set[str]] = {}


def _held(row: dict | None, owner: str) -> dict:
    return dict(row["doc"]) if (row and not owner and row.get("mandatory") and row.get("doc")) else {}


def _sync_index(items: list[CheckDoc], agent: str, owner: str, *,
                whole: bool = True, unseen: bool = False) -> list[CheckDoc]:
    """The index follows the files: changed hashes are rewritten (as
    "file"), rows of vanished folders are dropped, the row's provenance is
    read back onto the item. A MANDATORY agent check whose folder is gone
    keeps its row (the manager removes it on the Checks page) and comes
    back as a held item with its problem. ``whole`` False (one check,
    loaded by name) touches that check's row only: the others were not
    looked at, so none of them is missing. ``unseen``: the tree could not
    be looked at (the agent's folder away, the checks folder unreadable),
    so no row is dropped or counted as missing, and every mandatory one is
    held at once."""
    rows = {r["name"]: r for r in db_checks.list_index(agent, owner)}
    for it in items:
        row = rows.get(it.name)
        if row and row["doc_sha256"] == it.doc_sha256 and row["script_sha256"] == it.script_sha256:
            it.updated_by, it.updated_at = row["updated_by"], row["updated_at"]
            continue
        if it.problems:
            # A broken document is not indexed as valid; an existing row
            # stays until the file is fixed or removed (the routes list the
            # problem).
            if row:
                it.updated_by, it.updated_at = row["updated_by"], row["updated_at"]
                it.held = _held(row, owner)
            continue
        saved = db_checks.upsert_index(agent, owner, it.name, it.doc, doc_sha256=it.doc_sha256,
                                       script_sha256=it.script_sha256, updated_by="file")
        it.updated_by, it.updated_at = saved["updated_by"], saved["updated_at"]
    if not whole:
        return []
    present = {it.name for it in items}
    gone = set(rows) - present
    if unseen:
        held = sorted(n for n in gone if _held(rows[n], owner))
        problem = "the agent's checks cannot be read now (it was mandatory)"
    else:
        # A folder missing on ONE load may be a rename window (a sync
        # writing the document as temp-and-rename, a commit in flight): the
        # row is dropped only when the folder is missing on two loads in a
        # row, so a blink never turns a manager's provenance into "file".
        key = (agent, owner)
        twice = gone & _missing_once.get(key, set())
        _missing_once[key] = gone
        held = sorted(n for n in twice if _held(rows[n], owner))
        if twice - set(held):
            db_checks.delete_index_missing(agent, owner, present | (gone - twice) | set(held))
        problem = "the check's folder is missing (it was mandatory)"
    return [CheckDoc(agent=agent, owner=owner, name=n, doc={"name": n}, folder=checks_root(agent, owner) / n,
                     doc_sha256="", updated_by=rows[n]["updated_by"], updated_at=rows[n]["updated_at"],
                     problems=[problem], held=_held(rows[n], owner))
            for n in held]


def load_checks(agent: str, owner: str = "", *, sync_index: bool = True) -> list[CheckDoc]:
    """Every check in one tree, read from disk (the cheap stat-and-hash of a
    few small files), the index brought in step (with a held item for a
    mandatory check whose folder is gone). Synchronous."""
    root = checks_root(agent, owner)
    items: list[CheckDoc] = []
    folders: list[Path] = []
    # The agent's own folder not there, or a tree that could not be looked
    # at, answers nothing about its checks: the rows stay (``unseen``).
    unseen = True
    with contextlib.suppress(OSError):
        base, rel = _beneath(agent, root)
        if safe_fs.kind_beneath(base, "").kind == "dir":
            unseen = False
            try:
                step = next(safe_fs.walk_beneath(base, rel))
                folders = sorted(root / n for n in step.dirs if CHECK_NAME_RE.match(n))
            except (*_NO_SUCH_FILE, StopIteration):
                # The checks folder gone, a file or a link is no check at
                # all: the index still answers a held item for each
                # mandatory one.
                pass
            except OSError:
                unseen = True
    for folder in folders[:MAX_CHECKS_PER_TREE]:
        it = _read_folder(agent, owner, folder)
        if it is not None:
            items.append(it)
    if sync_index:
        try:
            items.extend(_sync_index(items, agent, owner, unseen=unseen))
        except Exception:  # noqa: BLE001  # the index is a cache: the checks still load from their files
            logger.warning("checks: index sync failed for %s/%s", agent, owner or "config",
                           exc_info=True)
    return items


def load_check(agent: str, owner: str, name: str) -> CheckDoc | None:
    folder = check_folder(agent, owner, name)
    it = _read_folder(agent, owner, folder)
    if it is not None:
        try:
            _sync_index([it], agent, owner, whole=False)
        except Exception:  # noqa: BLE001  # the index is a cache: the check still loads from its file
            logger.warning("checks: index sync failed for %s", folder, exc_info=True)
    return it


def resolve_ref(agent: str, ref: str, username: str) -> CheckDoc | None:
    """``agent:<name>`` or ``user:<name>`` (or a bare name: the agent's
    first, then the user's own) → the document, if it exists and is valid."""
    ref = (ref or "").strip()
    if ref.startswith("agent:"):
        candidates = [("", ref[6:])]
    elif ref.startswith("user:"):
        candidates = [(username, ref[5:])] if username else []
    else:
        candidates = [("", ref)] + ([(username, ref)] if username else [])
    for owner, name in candidates:
        if not CHECK_NAME_RE.match(name):
            continue
        it = load_check(agent, owner, name)
        if it is not None and not it.problems:
            return it
    return None


# ── writes ──────────────────────────────────────────────────────────────────


def script_as_written(text: str) -> str:
    """A check's script as its folder keeps it (a final newline added) —
    the text the index's ``script_sha256`` is taken over."""
    return text if text.endswith("\n") else text + "\n"


def judge_envelope_problem(agent: str, judge: dict) -> str | None:
    """Why a judge's ``engine`` and ``model`` are outside what ``agent`` has
    enabled (``spawn_authz.validate_spawn_overrides``, the rule a delegated
    worker's override meets), or None. A retired model is judged as its
    successor, the model the run would use."""
    engine = (judge or {}).get("engine") or None
    model = config.live_model_id((judge or {}).get("model") or "") or None
    if not (engine or model):
        return None
    from fastapi import HTTPException

    from services.delegation.spawn_authz import validate_spawn_overrides
    try:
        validate_spawn_overrides(agent, engine, model)
    except HTTPException as e:
        return f"judge: {e.detail}"
    return None


def write_check(agent: str, owner: str, doc, script: str | None, *, updated_by: str) -> CheckDoc:
    """Validate, write the folder (the document and, when given, the
    script), commit the config repo, and index. Synchronous; the caller
    fans the write out to satellites (``fan_out``)."""
    clean = validate_check_doc(doc, owner=owner)
    problem = judge_envelope_problem(agent, clean.get("judge") or {})
    if problem:
        raise CheckError(problem)
    name = clean["name"]
    folder = check_folder(agent, owner, name)
    base, rel = _beneath(agent, folder)
    run = (clean.get("script") or {}).get("run")
    if script is not None:
        if not isinstance(script, str):
            raise CheckError("script must be text")
        if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise CheckError("the script is larger than 256 KB")
        if not run:
            raise CheckError("a script text needs a script section naming its file")
    try:
        if run and script is None and not _plain_file(base, f"{rel}/{run}"):
            raise CheckError(f"the script {run} is not in the check's folder — send its text")
        # A link at either name is replaced, never written through; a link
        # on the way to the folder refuses the write.
        safe_fs.atomic_write_beneath(base, f"{rel}/{DOC_FILE}",
                                     canonical_json(clean).encode("utf-8"), mkdirs=True)
        if script is not None and run:
            safe_fs.atomic_write_beneath(base, f"{rel}/{run}",
                                         script_as_written(script).encode("utf-8"), mkdirs=True)
    except safe_fs.SymlinkRefused as e:
        raise CheckError(_LINKED) from e
    except OSError as e:
        raise CheckError("the check's files could not be written") from e
    doc_path = folder / DOC_FILE
    _commit(agent, owner, [doc_path] + ([folder / run] if (script is not None and run) else []),
            f"checks: {name} by {updated_by or 'platform'}")
    it = _read_folder(agent, owner, folder)
    if it is None:
        raise CheckError("the check's files could not be written")
    if it.problems:
        raise CheckError("; ".join(it.problems))
    saved = db_checks.upsert_index(agent, owner, name, it.doc, doc_sha256=it.doc_sha256,
                                   script_sha256=it.script_sha256, updated_by=updated_by or "")
    it.updated_by, it.updated_at = saved["updated_by"], saved["updated_at"]
    return it


def _plain_file(base: Path, rel: str) -> bool:
    return safe_fs.kind_beneath(base, rel).is_file


def delete_check(agent: str, owner: str, name: str) -> bool:
    """The folder and the index row; True when either existed (a held
    mandatory check has only its row left). A link at the check's name is
    removed and its target kept; a link on the way to it removes nothing
    on disk (the row still goes) and refuses with ``CheckError``."""
    folder = check_folder(agent, owner, name)
    base, rel = _beneath(agent, folder)
    try:
        st = safe_fs.lstat_beneath(base, rel)
    except FileNotFoundError:
        st = None
    except OSError as e:
        db_checks.delete_index(agent, owner, name)
        raise CheckError(_LINKED) from e
    if st is not None:
        try:
            if stat.S_ISDIR(st.st_mode):
                safe_fs.rmtree_beneath(base, rel, missing_ok=True)
            else:
                safe_fs.unlink_beneath(base, rel, missing_ok=True)
        except OSError:
            logger.warning("checks: the folder of %s/%s was not removed", agent, name, exc_info=True)
        _commit(agent, owner, [folder], f"checks: remove {name}")
    had_row = db_checks.delete_index(agent, owner, name)
    return st is not None or had_row


def _commit(agent: str, owner: str, paths: list[Path], message: str) -> None:
    """The agent's config repo tracks its checks (git_writer); a user's tree
    has no repo of its own beside context/, so a user's checks are not
    committed."""
    if owner:
        return
    try:
        from services.infra import git_writer
        repo = _agent_dir(agent) / "config"
        git_writer.commit_paths(repo, paths, message)
    except (ImportError, ValueError):
        logger.warning("checks: git commit failed for %s", agent, exc_info=True)


async def fan_out(agent: str, owner: str, name: str) -> None:
    """A written check reaches the satellites that may see the tree, as any
    platform write does (file_bookkeeping)."""
    folder = check_folder(agent, owner, name)
    base, rel = _beneath(agent, folder)
    try:
        from services.infra import file_bookkeeping
        step = next(safe_fs.walk_beneath(base, rel))
        for n in sorted(step.files):
            await file_bookkeeping.push_file_write(agent, f"{rel}/{n}", folder / n, writer=None)
    except Exception:  # noqa: BLE001  # the fan-out to machines is best effort, the files are written
        logger.debug("checks: fan-out skipped for %s", folder, exc_info=True)


def describe(it: CheckDoc) -> dict:
    """The row a route or a tool lists."""
    return {
        "ref": it.ref, "name": it.name, "owner": it.owner,
        "description": it.doc.get("description") or "",
        "mandatory": it.mandatory, "applies": it.applies,
        "condition": it.doc.get("condition") or {}, "rounds": it.rounds,
        "inputs": it.doc.get("inputs") or [], "sections": it.sections,
        "doc": it.doc, "doc_sha256": it.doc_sha256, "script_sha256": it.script_sha256,
        "updated_by": it.updated_by, "updated_at": it.updated_at,
        "problems": list(it.problems),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
