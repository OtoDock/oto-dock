"""Client-side builtin tools for Direct-LLM sessions.

The CLI engines ship their own file tools; the direct layer had none, so an
agent on a local model could not read a knowledge file. This registry adds
in-process tools with the CLI's names (persona and skill text ports
unchanged, the dashboard's tool cards already render them):

- ``Read`` / ``Glob`` — open tier (allowed in every mode)
- ``Write`` / ``Edit`` — edit tier (asked in Ask, silent in Don't Ask and
  unattended; a held Auto, which Direct LLM does not offer, runs them as
  Accept Edits did)
- ``Delete`` — destructive tier (asked in Ask and in a held Auto, exactly
  ``rm``'s treatment; silent only in Don't Ask and unattended)

Two-pass gate, mirroring ``api.hooks.permission.decide_tool_permission`` for the
CLI: Pass-1 is ``auth.path_policy.check_tool_access`` with the session's
SecurityContext (RBAC, memory files, credentials, library mirrors), Pass-2
is the tier × mode table above. Plan mode allows only the open tier. The
mount table (``core/layers/direct/files.py``) is the hard boundary
underneath — a viewer's read-only session has no writable root, so the
write tools refuse without prompting.

Later items of the same plan register ``Skill`` and ``tool_search`` here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.layers.direct import files as direct_files
from core.layers.direct.files import FileToolError, Resolved
from core.permission_modes import (
    AUTO_REVIEW_MODE,
    DONT_ASK_MODE,
    JUDGE_MODE,
    PLAN_MODE,
    UNATTENDED_MODE,
    current_mode,
)

logger = logging.getLogger("direct-runner")

TIER_OPEN = "open"
TIER_EDIT = "edit"
TIER_DESTRUCTIVE = "destructive"

Handler = Callable[[Any, dict], Awaitable[str]]


@dataclass(frozen=True)
class Builtin:
    name: str
    description: str
    input_schema: dict
    tier: str
    handler: Handler
    file_path_tool: bool = False  # Pass-1 path policy applies (file_path arg)


_REGISTRY: dict[str, Builtin] = {}


def register(builtin: Builtin) -> None:
    _REGISTRY[builtin.name] = builtin


def get(name: str) -> Builtin | None:
    return _REGISTRY.get(name)


def is_builtin(name: str) -> bool:
    return name in _REGISTRY


def tool_defs(names: tuple[str, ...] | list[str] | None = None) -> list[dict]:
    """Universal-format tool entries (``name`` / ``description`` /
    ``input_schema``) for the given builtins (all when ``names`` is None)."""
    picked = [_REGISTRY[n] for n in (names or list(_REGISTRY)) if n in _REGISTRY]
    return [
        {"name": b.name, "description": b.description, "input_schema": b.input_schema}
        for b in picked
    ]


FILE_TOOL_NAMES: tuple[str, ...] = ("Read", "Glob", "Write", "Edit", "Delete")
# Every client-side tool a direct session always carries (tool_search is
# added by the deferred-tools catalog only when deferral is active).
CLIENT_TOOL_NAMES: tuple[str, ...] = FILE_TOOL_NAMES + ("Skill",)
# The call tools: a phone session carries the first two (the CLI engines
# get them from the call-control MCP instead); the ask tool only on an
# outbound call another agent placed.
CALL_TOOL_NAMES: tuple[str, ...] = ("end_call", "transfer_call")
CALL_ASK_TOOL = "ask_initiating_agent"


def file_tool_defs() -> list[dict]:
    return tool_defs(FILE_TOOL_NAMES)


def client_tool_defs() -> list[dict]:
    return tool_defs(CLIENT_TOOL_NAMES)


def call_tool_defs(*, ask_back: bool) -> list[dict]:
    return tool_defs(CALL_TOOL_NAMES + ((CALL_ASK_TOOL,) if ask_back else ()))


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------

def decide(tier: str, mode: str) -> str:
    """Pass-2: ``"allow"`` | ``"prompt"`` | ``"deny"`` for a builtin tier in a
    session mode — the CLI hook's dashboard branch, tier by tier."""
    mode = current_mode(mode)
    if tier == TIER_OPEN:
        return "allow"
    if mode in (PLAN_MODE, JUDGE_MODE):
        return "deny"
    if mode in (DONT_ASK_MODE, UNATTENDED_MODE):
        return "allow"
    if tier == TIER_EDIT:
        return "allow" if mode == AUTO_REVIEW_MODE else "prompt"
    return "prompt"  # destructive in Ask / a held Auto


def gate(session, tool_call: dict, mode: str) -> tuple[str, str]:
    """Both passes for one builtin call → ``(outcome, reason)``; ``reason`` is
    the tool-result text for a deny (empty otherwise)."""
    name = tool_call.get("name", "")
    builtin = _REGISTRY.get(name)
    if builtin is None:
        return "deny", f"Unknown tool '{name}'"
    args = tool_call.get("input") or {}
    if builtin.file_path_tool:
        from auth.path_policy import check_tool_access
        from core.session.session_state import get_session_security
        ctx = get_session_security(session.session_id)
        if ctx is None:
            # Fail closed like the hook: a live session always has a context.
            return "deny", "Session is no longer active. Send a new message to continue."
        decision, _ = check_tool_access(name, args, ctx)
        if not decision.allowed:
            return "deny", decision.reason or f"{name} denied by the session's file policy"
    outcome = decide(builtin.tier, mode)
    if outcome == "deny":
        if mode == "judge":
            return "deny", f"{name} is not available to a judge — a judge reads, it does not write."
        return "deny", f"{name} is not available in plan mode (reads only)."
    return outcome, ""


MEETING_ENDED = "The meeting this session belonged to has ended."
# The permission authority's answer that lets a call run.
_AUTHORITY_ALLOWS = "allow"


async def gate_for_session(session, tool_call: dict, mode: str) -> tuple[str, str]:
    """``gate`` as the session's place in a meeting decides it. A mapped
    meeting participant's call goes to the one permission authority
    (``session_events.pre_tool``), as its MCP tools and every tool of a CLI
    participant do: the host chat's mode, the turn-end backstop after a
    routing tool, the ask on the meeting pump's queue with the speaker's
    badge, the card retired when nobody answers. A session started for a
    meeting whose mapping is gone (the meeting ended, the agent left) is
    refused every call. Any other session: ``gate``."""
    from core.events import tool_roles
    from core.session import session_events
    from core.session.session_state import get_meeting_session_info, is_meeting_session

    sid = session.session_id
    name = tool_call.get("name", "") or ""
    if get_meeting_session_info(sid) is not None and tool_roles.role_of(name):
        decision = await session_events.pre_tool(sid, name, tool_call.get("input") or {})
        if decision.get("decision") == _AUTHORITY_ALLOWS:
            return "allow", ""
        return "deny", decision.get("reason") or f"{name} was not allowed in this meeting."
    if is_meeting_session(sid) and get_meeting_session_info(sid) is None:
        return "deny", MEETING_ENDED
    return gate(session, tool_call, mode)


async def execute(session, name: str, args: dict) -> str:
    """Run a builtin in-process; every failure becomes result text."""
    builtin = _REGISTRY.get(name)
    if builtin is None:
        return f"Error: Unknown tool '{name}'"
    try:
        return await builtin.handler(session, args or {})
    except FileToolError as e:
        return f"Error: {e}"
    except Exception as e:  # never let a tool bug kill the turn
        logger.exception("builtin %s failed", name)
        return f"Error: {name} failed: {e}"


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

def _resolve(session, raw: str, *, writing: bool) -> Resolved:
    cfg = getattr(session, "sandbox_cfg", None)
    if cfg is None:
        raise FileToolError("file tools are not available in this session (no sandbox)")
    mounts = session.mount_table()
    return direct_files.resolve(
        mounts, raw, cwd=direct_files.session_cwd(cfg), writing=writing,
    )


def _agent_dir(session):
    cfg = session.sandbox_cfg
    return cfg.host_agents_dir / cfg.agent_name


def _writer(session) -> str | None:
    """The author recorded for a platform write: the session's REAL username
    (``""`` for agent-scope / Shared-only sessions → None) — the same value a
    CLI write would be attributed to at the turn-end scan."""
    from core.session.session_state import get_session_security
    ctx = get_session_security(session.session_id)
    return (getattr(ctx, "username", "") or None) if ctx else None


async def _after_write(session, res: Resolved) -> None:
    """The platform write bookkeeping (tombstone retire, author, library
    projection, satellite fan-out) — best-effort after a successful write."""
    from services.infra import file_bookkeeping
    cfg = session.sandbox_cfg
    try:
        rel = res.host.resolve().relative_to(_agent_dir(session).resolve()).as_posix()
    except (OSError, ValueError):
        return
    try:
        await file_bookkeeping.push_file_write(
            cfg.agent_name, rel, res.host, writer=_writer(session),
        )
    except Exception:
        logger.exception("write bookkeeping failed for %s", res.virtual)


async def _read(session, args: dict) -> str:
    res = _resolve(session, args.get("file_path", ""), writing=False)
    return await asyncio.to_thread(
        direct_files.read_numbered, res, args.get("offset"), args.get("limit"),
    )


async def _glob(session, args: dict) -> str:
    cfg = session.sandbox_cfg
    raw = args.get("path") or (direct_files.session_cwd(cfg) if cfg else "")
    res = _resolve(session, raw, writing=False)
    paths = await asyncio.to_thread(direct_files.glob_paths, res, args.get("pattern", ""))
    if not paths:
        return "(no matches)"
    out = "\n".join(paths)
    if len(paths) >= direct_files.GLOB_MAX_ENTRIES:
        out += f"\n... (stopped at {direct_files.GLOB_MAX_ENTRIES} entries — narrow the pattern)"
    return out


async def _write(session, args: dict) -> str:
    res = _resolve(session, args.get("file_path", ""), writing=True)
    existed = res.host.exists()
    n = await asyncio.to_thread(direct_files.write_text, res, args.get("content", ""))
    await _after_write(session, res)
    return f"{'Updated' if existed else 'Created'} {res.virtual} ({n} bytes)"


async def _edit(session, args: dict) -> str:
    res = _resolve(session, args.get("file_path", ""), writing=True)
    count = await asyncio.to_thread(
        direct_files.edit_text, res,
        args.get("old_string", ""), args.get("new_string", ""),
        bool(args.get("replace_all", False)),
    )
    await _after_write(session, res)
    return f"Edited {res.virtual} ({count} replacement{'s' if count != 1 else ''})"


async def _delete(session, args: dict) -> str:
    res = _resolve(session, args.get("file_path", ""), writing=True)
    host = res.host
    if host.is_symlink():
        raise FileToolError(f"{res.virtual} is a link — Delete removes regular files only")
    if not host.exists():
        raise FileToolError(f"File not found: {res.virtual}")
    if host.is_dir():
        raise FileToolError(f"{res.virtual} is a folder — Delete removes single files")
    from services.infra import file_bookkeeping
    cfg = session.sandbox_cfg
    skipped = await file_bookkeeping.delete_platform_file(
        cfg.agent_name, _agent_dir(session), host.resolve(),
    )
    if skipped:
        return f"Deleted {res.virtual} (too large for the Recover bin — this cannot be undone)"
    return f"Deleted {res.virtual} (moved to the Recover bin)"


# ---------------------------------------------------------------------------
# Skill — the direct layer's activation surface for on-demand skills
# ---------------------------------------------------------------------------

def _skills_dir(session):
    cfg = getattr(session, "sandbox_cfg", None)
    if cfg is None:
        raise FileToolError("skills are not available in this session (no sandbox)")
    return Path(cfg.host_claude_dir) / "skills"


def _available_skills(skills_dir) -> list[str]:
    if not skills_dir.is_dir():
        return []
    return sorted(
        p.name for p in skills_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "SKILL.md").is_file()
    )


def _read_skill(skills_dir, name: str) -> str:
    from services.mcp.mcp_manifest_types import SKILL_ID_MAX_LEN, SKILL_ID_RE
    from services.mcp.skill_format import strip_frontmatter
    available = _available_skills(skills_dir)
    hint = (
        f" Available: {', '.join(available)}" if available
        else " No on-demand skills are enabled for this session."
    )
    if not name or not SKILL_ID_RE.fullmatch(name) or len(name) > SKILL_ID_MAX_LEN:
        raise FileToolError(f"name must be a skill id from the # Skills list.{hint}")
    path = skills_dir / name / "SKILL.md"
    if not path.is_file():
        raise FileToolError(f"unknown skill '{name}'.{hint}")
    body = strip_frontmatter(path.read_text(encoding="utf-8", errors="replace")).strip()
    return f"# Skill: {name}\n\n{body}" if body else f"# Skill: {name}\n\n(empty skill)"


def _load_guide_tools(session, skill_id: str, body: str) -> str:
    """When the skill's provider MCP has DEFERRED tools that the guide names,
    load their definitions now and say so in a footer. The model asked for
    the guide because it is about to call those tools; without this a weaker
    model reads "use `write_xlsx`", finds no such tool in its list, and
    reaches for whatever it has (seen live 2026-09-06 on a local model).
    D8 forbids injecting guide BODIES on load, not loading schemas."""
    catalog = getattr(session, "catalog", None)
    if catalog is None or not catalog.deferred:
        return ""
    try:
        from services.mcp import mcp_registry
        manifest = mcp_registry.find_skill_provider(skill_id)
    except ImportError:
        return ""
    if manifest is None:
        return ""
    from core.layers.direct.tool_catalog import bare_tool, server_of
    server = getattr(manifest, "server_name", "") or manifest.name
    mentioned = [
        n for n in catalog.deferred
        if server_of(n) == server and bare_tool(n) in body
    ]
    if not mentioned:
        return ""
    from core.layers.direct.tool_catalog import batch_loads_for
    new_defs = catalog.load(
        mentioned, whole_server=batch_loads_for(getattr(session, "provider", "")),
    )
    session.tools.extend(new_defs)
    if not new_defs:
        return ""
    return (
        "\n\n---\nLoaded the tools this guide describes — call them directly now: "
        + ", ".join(f"`{t['name']}`" for t in new_defs)
    )


async def _skill(session, args: dict) -> str:
    """Return the body of a materialized on-demand skill
    (``<config dir>/skills/<id>/SKILL.md`` — the same tree
    ``skills_materializer`` lays down for every session config dir), with
    the frontmatter stripped. An unknown name lists what is available. When
    the guide's own MCP has deferred tools the guide names, they are loaded
    as a side effect (footer says which)."""
    name = str(args.get("name") or "").strip()
    body = await asyncio.to_thread(_read_skill, _skills_dir(session), name)
    return body + _load_guide_tools(session, name, body)


register(Builtin(
    name="Skill",
    description=(
        "Load the full instructions of an on-demand skill listed under "
        "`# Skills` in your instructions. Do this before work that skill "
        "covers; the guide text comes back as the result."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill id from the # Skills list"},
        },
        "required": ["name"],
    },
    tier=TIER_OPEN,
    handler=_skill,
    file_path_tool=False,
))


# ---------------------------------------------------------------------------
# tool_search — the deferred-tools loader (core/layers/direct/tool_catalog.py)
# ---------------------------------------------------------------------------

def _guides_available(session, guide_ids: list[str]) -> list[str]:
    """Only the guides this session can actually load with `Skill`."""
    try:
        available = set(_available_skills(_skills_dir(session)))
    except FileToolError:
        return []
    return [g for g in guide_ids if g in available]


async def _tool_search(session, args: dict) -> str:
    from core.layers.direct.tool_catalog import (
        batch_loads_for,
        first_sentence,
        server_of,
    )
    catalog = getattr(session, "catalog", None)
    if catalog is None:
        return (
            "tool_search is not active in this session — every tool is "
            "already loaded; call tools directly."
        )
    query = str(args.get("query") or "").strip()
    if not query:
        raise FileToolError("query is required (keywords, or select:<name>,<name>)")
    try:
        max_results = int(args.get("max_results") or 0)
    except (TypeError, ValueError):
        max_results = 0
    names = catalog.search(query, max_results or 5)
    if not names:
        return (
            f"No deferred tools match {query!r}. Try other keywords, or "
            "`select:<exact name>` with a name from the # Deferred tools list "
            "in your instructions."
        )
    batched = batch_loads_for(getattr(session, "provider", ""))
    new_defs = catalog.load(names, whole_server=batched)
    session.tools.extend(new_defs)
    new_names = {t["name"] for t in new_defs}
    lines = [
        f"Loaded {len(new_defs)} tool(s) — available from now on:"
        if new_defs else "Already loaded:"
    ]
    # On a local server the whole server's tools came along (one context
    # re-process instead of one per tool) — list the extras too, briefly.
    extra = [t["name"] for t in new_defs if t["name"] not in names]
    if extra:
        lines.append(
            "Also loaded from the same server(s): " + ", ".join(f"`{n}`" for n in extra)
        )
    guide_lines: list[str] = []
    seen_servers: set[str] = set()
    for n in names:
        desc = first_sentence(catalog.deferred[n].get("description", ""))
        suffix = "" if n in new_names else " (already loaded)"
        lines.append(f"- `{n}`{' — ' + desc if desc else ''}{suffix}")
        server = server_of(n)
        if server not in seen_servers:
            seen_servers.add(server)
            guides = _guides_available(session, catalog.guides.get(server) or [])
            if guides:
                guide_lines.append(
                    f"guide for {server}: " + ", ".join(f"`{g}`" for g in guides)
                    + " (Skill tool)"
                )
    return "\n".join(lines + guide_lines)


def _register_tool_search() -> None:
    from core.layers.direct.tool_catalog import tool_search_def
    d = tool_search_def()
    register(Builtin(
        name=d["name"], description=d["description"], input_schema=d["input_schema"],
        tier=TIER_OPEN, handler=_tool_search, file_path_tool=False,
    ))


_register_tool_search()


# ---------------------------------------------------------------------------
# Call tools — the phone session's end / transfer / ask, in-process
# ---------------------------------------------------------------------------

async def _call_action(session, action: str, args: dict) -> str:
    """One call action through the call-control registry; the daemon's
    verdict is the tool's text whether it accepted or refused. An accepted
    end or transfer marks the session so the tool loop ends the turn without
    another model call (the line closes as the farewell drains)."""
    from services.phone import call_control
    result = await call_control.request(session.session_id, action, args)
    message = result.get("message") or ""
    if result.get("ok"):
        if action in (call_control.ACTION_END, call_control.ACTION_TRANSFER):
            session.line_closing = True
        return message
    return f"Not done: {message}" if message else "Not done."


async def _end_call(session, args: dict) -> str:
    return await _call_action(session, "end", {"reason": str(args.get("reason") or "")[:200]})


async def _transfer_call(session, args: dict) -> str:
    target = str(args.get("target") or "").strip()
    if not target:
        raise FileToolError("target is required: a transfer target's name from your instructions")
    return await _call_action(session, "transfer", {"target": target[:80]})


async def _ask_initiating_agent(session, args: dict) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        raise FileToolError("question is required")
    return await _call_action(session, "ask", {"question": question[:1000]})


register(Builtin(
    name="end_call",
    description=(
        "End the phone call you are on. Say your goodbye in one short sentence "
        "BEFORE calling this; the line closes right after and nothing you write "
        "afterwards is heard. Only during a live call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why the call ends, in a few words (for the log)"},
        },
    },
    tier=TIER_OPEN,
    handler=_end_call,
    file_path_tool=False,
))

register(Builtin(
    name="transfer_call",
    description=(
        "Hand the caller to one of the transfer targets named in your instructions. "
        "Tell the caller in one short sentence FIRST, then call this with the target's "
        "name; you leave the call. Only during a live call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "The target's name (or number) from your instructions"},
        },
        "required": ["target"],
    },
    tier=TIER_OPEN,
    handler=_transfer_call,
    file_path_tool=False,
))

register(Builtin(
    name=CALL_ASK_TOOL,
    description=(
        "Ask the agent that placed this call a question it can answer (a "
        "preference, a fact you lack). Say that you are checking (one short "
        "sentence) and stop: the answer arrives as your next message within "
        "about 40 seconds."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, in one or two sentences"},
        },
        "required": ["question"],
    },
    tier=TIER_OPEN,
    handler=_ask_initiating_agent,
    file_path_tool=False,
))


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

_PATH_DESC = (
    "Path in the session's folders: absolute virtual form (/workspace/..., "
    "/knowledge/..., /users/<you>/..., /config/...) or relative to the session folder."
)

register(Builtin(
    name="Read",
    description=(
        "Read a text file from the session's folders. Returns numbered lines "
        "(default: the first 2000). Text only — documents, spreadsheets and "
        "images go through the file-tools MCP."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": _PATH_DESC},
            "offset": {"type": "integer", "description": "1-based line number to start from"},
            "limit": {"type": "integer", "description": "Maximum number of lines to return"},
        },
        "required": ["file_path"],
    },
    tier=TIER_OPEN,
    handler=_read,
    file_path_tool=True,
))

register(Builtin(
    name="Glob",
    description=(
        "List files matching a glob pattern (e.g. **/*.md) under a folder of "
        "the session. Returns virtual paths, sorted, at most 200."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern relative to the folder, e.g. **/*.md or reports/*.csv"},
            "path": {"type": "string", "description": "Folder to search (default: the session folder). " + _PATH_DESC},
        },
        "required": ["pattern"],
    },
    tier=TIER_OPEN,
    handler=_glob,
    file_path_tool=True,  # check_tool_access reads Glob's optional `path`
))

register(Builtin(
    name="Write",
    description=(
        "Create or overwrite a text file in a writable folder of the session "
        "(parent folders are created). Up to 1 MB of UTF-8 text."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": _PATH_DESC},
            "content": {"type": "string", "description": "The full file content"},
        },
        "required": ["file_path", "content"],
    },
    tier=TIER_EDIT,
    handler=_write,
    file_path_tool=True,
))

register(Builtin(
    name="Edit",
    description=(
        "Replace an exact string in a text file. old_string must match "
        "exactly and be unique in the file unless replace_all is true."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": _PATH_DESC},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    tier=TIER_EDIT,
    handler=_edit,
    file_path_tool=True,
))

register(Builtin(
    name="Delete",
    description=(
        "Delete one file from a writable folder of the session. The file "
        "goes to the Recover bin (dashboard → Files) unless it is too large."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": _PATH_DESC},
        },
        "required": ["file_path"],
    },
    tier=TIER_DESTRUCTIVE,
    handler=_delete,
    file_path_tool=True,
))
