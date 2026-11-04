"""Internal memory API — the ``memory`` MCP tool's callback surface.

``POST /v1/internal/memory/op`` executes ONE memory-tool command against the
calling session's memory scopes, mirroring the Anthropic ``memory_20250818``
contract (view / create / str_replace / insert / delete / rename). The role
matrix + toggles are enforced here; file semantics + the verbatim contract
strings live in ``services/memory/memory_file``.

Tool paths are virtual and scope-prefixed:

    /memories/agent/...  →  agents/{agent}/knowledge/memory/...
    /memories/user/...   →  agents/{agent}/users/{username}/context/memory/...

Role matrix (server-enforced; editors collaborate on agent memory by design —
they extend the agent's *knowledge*, while ``config/`` behaviour stays
manager-only):

| session/role         | user scope     | agent scope |
|----------------------|----------------|-------------|
| viewer (user session)| read/write own | read-only   |
| editor               | read/write own | read/write  |
| manager / admin      | read/write own | read/write  |
| agent-scope session  | not available  | read/write  |

Response shape: command outcomes — INCLUDING contract-level errors such as
"Error: File x already exists" — return HTTP 200 with ``{output, is_error,
warnings}`` and the MCP relays ``output`` verbatim as the tool-result text
(models are trained on those strings). HTTP errors are reserved for
auth-shape failures: 401/403 bad caller, 404 unknown agent, 400 malformed
request.

Every successful mutation:
  1. regenerates the scope's ``MEMORY.md`` index (inside ``memory_file``),
  2. git-commits the touched paths as ONE attributed commit,
  3. publishes platform-write bookkeeping + live propagation — tombstone
     retire/record, ``file_author`` attribution, satellite fan-out, dashboard
     ``file_updated`` broadcast — mirroring the dashboard file API so tool
     writes reach open satellites and Files-UI views mid-session
     (``services/memory/memory_publish``).

A ``rename`` from ``/memories/user/`` to ``/memories/agent/`` is a
PROMOTION (``services/memory/memory_promote``): editor and above, two
attributed commits, both roots published; ``POST /promote`` is the same
function behind a signed-in person's role, and the chat's "promote a
memory" card (``api/cards/actions_team``) calls the function itself. The
other direction is refused.

Settings endpoints (admin / manager, dashboard cookies) and the danger-zone
``clear-all`` / ``clear-my-memory`` remain at the bottom.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

import config
from auth import roles
from auth.providers import UserContext, acting_role_of, get_current_user, require_auth
from core import layout
from core.session.visibility import SCOPE_AGENT, SCOPE_USER
from services.memory import memory_file, memory_nudge, memory_promote, memory_publish
from services.memory.memory_file import MemoryCapError, MemoryOpError, OpResult
from services.memory.memory_promote import MemoryRoleError
from storage import database as task_store
from storage.agents import agent_store, memory_store

logger = logging.getLogger("otodock.memory-api")
router = APIRouter(prefix="/v1/internal/memory")

_AGENT_GONE = "This agent no longer exists."
_NO_SUCH_SETTING = "That memory setting does not exist."
_NOT_A_PERSON = "Only a person signed in can share a memory topic."
_NOT_YOUR_TIER = (
    "Only editors and managers can share a memory with everyone on this "
    "agent. Your topic stays private."
)
_NOT_PERSONAL_PATH = "Only a topic under /memories/user/ can be shared."
_NOT_SHARED_PATH = "A shared topic's new name must be under /memories/agent/."
_NEW_PATH_SHAPE = "new_path must name a topic under /memories/agent/ or /memories/user/."
_DEMOTION = (
    "Error: rename cannot move a topic from the shared scope to a personal "
    "one. Shared knowledge stays shared: copy what is personal into "
    "/memories/user/ and delete it from the shared topic."
)

# The numeric settings and what the route accepts for each (an int that is
# no bool, inside the range; None means no upper bound).
_INT_MAX = 2**31 - 1   # the columns are INTEGER
_SETTING_RANGES: dict[str, tuple[int, int]] = {
    "inline_budget_bytes": (1, _INT_MAX),
    "nudge_turns": (0, _INT_MAX),
    "topic_grow_cap_bytes": memory_file.GROW_CAP_RANGE,
    "entry_cap_bytes": memory_file.ENTRY_CAP_RANGE,
}
_SETTING_REFUSALS = {
    "inline_budget_bytes": "The inline budget must be a whole number of bytes, at least 1.",
    "nudge_turns": "The turns before a nudge must be a whole number.",
    "topic_grow_cap_bytes": "The growth cap must be a whole number of bytes from 16,384 to 65,536.",
    "entry_cap_bytes": "The entry cap must be a whole number of bytes from 256 to 65,536.",
}


_COMMANDS = {"view", "create", "str_replace", "insert", "delete", "rename"}
_WRITE_COMMANDS = {"create", "str_replace", "insert", "delete", "rename"}


# ---------------------------------------------------------------------------
# Role + toggle helpers
# ---------------------------------------------------------------------------

def _resolve_effective_role(user_sub: str, agent: str) -> str:
    """Real role at this (user, agent) pair, ignoring ``is_api_key`` inflation.

    Returns an effective role (``roles.EFFECTIVE_ROLES``), or
    ``roles.SERVICE`` for agent-scope service sessions with no user owner.
    """
    if not user_sub:
        return roles.SERVICE
    return acting_role_of(user_sub, agent)


def _require_mcp_caller(user: UserContext, agent: str) -> None:
    """Memory-mcp callback endpoints accept only session-JWT auth."""
    if not user.is_api_key:
        raise HTTPException(403, "memory endpoint requires session API key")
    if not agent:
        raise HTTPException(400, "X-Agent-Name required")


def _require_agent_match(user: UserContext, agent: str) -> None:
    """The calling session must belong to the agent it names.

    Token-authoritative: a session minted for agent A cannot read or write
    agent B's memory by naming B in ``X-Agent-Name`` (the header is a hint,
    the ``agent`` claim is the fact); a user-backed session must have access
    to the agent. The master key (memory consolidation) is unrestricted.
    Runs AFTER the agent-existence check so an unknown agent stays a 404.
    """
    if user.is_session and user.agent and agent != user.agent:
        raise HTTPException(403, "session token is not for this agent")
    if user.acting_sub is not None and not user.can_access_agent(agent):
        raise HTTPException(403, "no access to this agent")


def _scope_enabled(scope: str, settings: dict[str, Any], toggles: dict[str, Any]) -> bool:
    """Master AND per-agent toggle compose."""
    key = f"{scope}_memory_enabled"
    return bool(settings.get(key) and toggles.get(key))


def _scope_unavailable(scope: str, *, external: bool, username: str | None) -> str:
    """Why a scope the session lacks is not there (the tool's text)."""
    if external and scope == SCOPE_AGENT:
        return (
            "Shared agent memory is not available on external routes "
            "— caller memories live under /memories/user/."
        )
    if external and scope == SCOPE_USER:
        return "This route keeps no per-caller memory."
    if scope == SCOPE_USER and not username:
        return (
            "user-scope memory is not available in this session "
            "(no user owner). Use /memories/agent/ instead."
        )
    return f"The {scope} memory scope is disabled for this agent."


def _external_caller_home(agent: str, agent_dir: Path, claim: str) -> Path | None:
    """The caller tree a session's signed ``ext`` claim points at, or None
    when the session has no tree (shared mode / withheld without a tree) or
    the agent's visibility mode offers no personal scope (Shared-only)."""
    from core.session.external_identity import home_from_claim
    from core.session.visibility import available_scopes_for
    row = agent_store.get_agent(agent) or {}
    if "user" not in available_scopes_for(
            bool(row.get("collaborative", True)), row.get("default_scope") or "user"):
        return None
    try:
        return home_from_claim(agent_dir, claim)
    except ValueError:
        logger.error("memory: malformed external claim %r on agent %s", claim, agent)
        return None


def _err(output: str, warnings: list[str] | None = None) -> dict[str, Any]:
    return {"output": output, "is_error": True, "warnings": warnings or []}


def _ok(result: OpResult) -> dict[str, Any]:
    return {
        "output": result.output,
        "is_error": False,
        "warnings": result.warnings,
    }


def _log_outcome(
    agent: str, scope: str, command: str, rel: str,
    *, refusal: MemoryCapError | None = None, result: OpResult | None = None,
) -> None:
    """The size caps leave a trace an operator can count: a refusal and a
    topic left over the soft target log at INFO with the agent, the scope,
    the topic and the size (in the text)."""
    if refusal is not None:
        logger.info(
            "memory: %s on %s refused (agent %s, %s scope): %s",
            command, rel, agent, scope, refusal,
        )
    elif result is not None and result.over_target:
        logger.info(
            "memory: %s on %s (agent %s, %s scope) left it at %d bytes, over the target",
            command, rel, agent, scope, result.over_target,
        )


# ---------------------------------------------------------------------------
# The /op endpoint
# ---------------------------------------------------------------------------

@router.post("/op")
async def memory_op(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
    x_on_behalf_of: str | None = Header(None, alias="X-On-Behalf-Of"),
) -> dict[str, Any]:
    """Execute one memory-tool command. Body: ``{command, path, ...args}``.

    Command args by command (mirrors ``memory_20250818``):
      view: ``path``, optional ``view_range: [start, end]``
      create: ``path``, ``file_text``
      str_replace: ``path``, ``old_str``, ``new_str``
      insert: ``path``, ``insert_line``, ``insert_text``
      delete: ``path``
      rename: ``old_path``, ``new_path``
    """
    u = require_auth(user)
    _require_mcp_caller(u, x_agent_name or "")
    agent = x_agent_name or ""
    if not agent_store.agent_exists(agent):
        raise HTTPException(404, f"agent not found: {agent}")
    _require_agent_match(u, agent)

    body = await request.json()
    command = body.get("command")
    if command not in _COMMANDS:
        # Contract outcome, not an HTTP error (see module docstring): a 400
        # reaches the model wrapped as "API error 400: {...}" — the 200 shape
        # is what lets it self-correct.
        return _err(
            f"unknown command: {command!r} (expected one of {sorted(_COMMANDS)})"
        )

    # Identity is token-authoritative: a session-JWT caller's acting user comes
    # from the token, NOT the client X-On-Behalf-Of header — so a no-user /
    # phone session resolves to agent scope only and can never reach another
    # user's per-user memory. Only the master key (trusted s2s, e.g. memory
    # consolidation) may target a specific user via X-On-Behalf-Of.
    if u.sub == "api-key":
        user_sub = x_on_behalf_of or ""
    else:
        user_sub = u.acting_sub or ""
    username: str | None = None
    if user_sub:
        username = task_store.get_username_by_sub(user_sub)
    role = _resolve_effective_role(user_sub, agent)
    agent_dir = config.get_agent_dir(agent)

    # Scopes available to THIS session (existence is lazy — roots may not
    # exist on disk yet; that's fine for view/create).
    #
    # EXTERNAL session (a phone caller who is not a platform user): never
    # the shared agent scope; the caller's own tree (from the signed token
    # claim) is the user scope when the agent's mode has one and the route
    # gave the caller a tree — same toggles as a user's memory.
    external = u.is_external
    settings = await asyncio.to_thread(memory_store.get_settings)
    toggles = await asyncio.to_thread(memory_store.get_agent_toggles, agent)
    caps = memory_file.Caps.from_settings(settings)
    scopes: dict[str, Path] = {}
    if _scope_enabled("agent", settings, toggles) and not external:
        scopes["agent"] = memory_file.scope_root(agent_dir, "agent")
    if external:
        role = "external"
        caller_home = _external_caller_home(agent, agent_dir, u.external_claim)
        if caller_home is not None and _scope_enabled("user", settings, toggles):
            scopes["user"] = caller_home / "context" / "memory"
    elif username and _scope_enabled("user", settings, toggles):
        scopes["user"] = memory_file.scope_root(agent_dir, "user", username)
    if not scopes:
        if external:
            return _err(
                "Memory is not available on this line: shared agent memory "
                "is never available on external routes, and this route keeps "
                "no per-caller memory."
            )
        return _err("Memory is disabled for this agent.")

    # rename carries two paths; both must land in the SAME scope.
    raw_path = body.get("old_path") if command == "rename" else body.get("path")
    if not raw_path or not isinstance(raw_path, str):
        return _err("path required")

    try:
        scope, rel = memory_file.split_virtual_path(raw_path)

        # `view /memories` — list available scopes.
        if scope == "":
            if command != "view":
                return _err(
                    "Only the `view` command works on /memories itself. "
                    "Target a scope path like /memories/"
                    f"{next(iter(scopes))}/topic.md"
                )
            for s in scopes.values():
                if memory_file.index_is_stale(s):
                    await asyncio.to_thread(memory_file.heal_index_if_stale, s)
            return _ok(memory_file.view_root(scopes))

        if scope not in scopes:
            return _err(_scope_unavailable(scope, external=external, username=username))

        # Role gate: writes to agent scope need editor+ (or an agent-scope
        # service session); user scope is always the caller's own.
        if command in _WRITE_COMMANDS and scope == "agent":
            if not (roles.can_edit(role) or role == roles.SERVICE):
                return _err(
                    "agent memory is read-only for your role (editors and "
                    "managers write it) — save user-scope memories under "
                    "/memories/user/ instead."
                )

        root = scopes[scope]
        rel = memory_file.validate_rel(
            rel,
            mutating=command in _WRITE_COMMANDS,
            require_ext=command == "create",
        )

        result: OpResult
        if command == "view":
            view_range = body.get("view_range")
            result = await asyncio.to_thread(
                memory_file.op_view, root, rel, view_range,
            )
        elif command == "create":
            result = await asyncio.to_thread(
                memory_file.op_create, root, rel, body.get("file_text") or "",
                caps=caps,
            )
        elif command == "str_replace":
            result = await asyncio.to_thread(
                memory_file.op_str_replace, root, rel,
                body.get("old_str") or "", body.get("new_str") or "",
                caps=caps,
            )
        elif command == "insert":
            insert_line = body.get("insert_line")
            if not isinstance(insert_line, int):
                raise HTTPException(400, "insert_line (integer) required")
            result = await asyncio.to_thread(
                memory_file.op_insert, root, rel, insert_line,
                body.get("insert_text") or "", caps=caps,
            )
        elif command == "delete":
            result = await asyncio.to_thread(memory_file.op_delete, root, rel)
        else:  # rename
            new_raw = body.get("new_path")
            if not new_raw or not isinstance(new_raw, str):
                raise HTTPException(400, "new_path required")
            new_scope, new_rel = memory_file.split_virtual_path(new_raw)
            if not new_scope or not new_rel:
                return _err(_NEW_PATH_SHAPE)
            if new_scope != scope:
                # Across the scopes: a promotion, or nothing. The destination
                # must be a scope this session has (an external caller or a
                # session with no user never reaches the promotion).
                if new_scope not in scopes:
                    return _err(_scope_unavailable(
                        new_scope, external=external, username=username,
                    ))
                if scope == SCOPE_AGENT:
                    return _err(_DEMOTION)
                new_rel = memory_file.validate_rel(new_rel, mutating=True)
                try:
                    result = await memory_promote.promote(
                        agent, username or "", rel, role=role, user_sub=user_sub,
                        dst_rel=new_rel, caps=caps,
                    )
                except MemoryRoleError as e:
                    return _err(str(e))
                except MemoryCapError as e:
                    _log_outcome(agent, new_scope, command, new_rel, refusal=e)
                    return _err(str(e))
                _log_outcome(agent, new_scope, command, new_rel, result=result)
                if u.session_id:
                    memory_nudge.record_memory_call(u.session_id)
                return _ok(result)
            new_rel = memory_file.validate_rel(new_rel, mutating=True)
            result = await asyncio.to_thread(
                memory_file.op_rename, root, rel, new_rel,
            )
    except MemoryCapError as e:
        _log_outcome(agent, scope, command, rel, refusal=e)
        return _err(str(e))
    except MemoryOpError as e:
        return _err(str(e))

    if result.changed or result.deleted:
        _log_outcome(agent, scope, command, rel, result=result)
        if not external:
            # A caller's tree is not a git repo (no per-user context repo
            # there) — the memory index + tombstones are its history.
            writer = username or "agent-session"
            await asyncio.to_thread(
                memory_publish.commit, agent_dir, scope, username, result,
                command, writer,
            )
        await memory_publish.publish_changes(
            agent, agent_dir, root, result,
            writer_slug=username, exclude_user_sub=user_sub,
        )
        # A landed write is memory maintenance: the capture nudge starts
        # over. A view, or a refused write, is not.
        if u.session_id:
            memory_nudge.record_memory_call(u.session_id)
    return _ok(result)


@router.post("/promote")
async def promote_endpoint(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Share one of the person's own memory topics with the agent: the
    promotion behind the person's role (editor and above on the agent).
    Body ``{agent, path, new_path?}``: ``path`` under ``/memories/user/``,
    ``new_path`` under ``/memories/agent/`` when the shared name differs.
    Answers the ``/op`` shape; the role refusal is a 403."""
    u = require_auth(user)
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    agent = body.get("agent") if isinstance(body.get("agent"), str) else ""
    if not agent or not agent_store.agent_exists(agent):
        raise HTTPException(404, _AGENT_GONE)
    _require_agent_match(u, agent)
    sub = u.acting_sub or ""
    if not sub:
        raise HTTPException(400, _NOT_A_PERSON)
    username = await asyncio.to_thread(task_store.get_username_by_sub, sub)
    if not username:
        raise HTTPException(400, "Could not find your account. Sign out, sign in again and retry.")
    role = await asyncio.to_thread(_resolve_effective_role, sub, agent)
    raw_path = body.get("path")
    new_raw = body.get("new_path")
    if not raw_path or not isinstance(raw_path, str):
        return _err("path required")
    try:
        scope, rel = memory_file.split_virtual_path(raw_path)
        if scope != SCOPE_USER:
            return _err(_NOT_PERSONAL_PATH)
        rel = memory_file.validate_rel(rel, mutating=True)
        dst_rel = rel
        if new_raw and isinstance(new_raw, str):
            new_scope, new_rel = memory_file.split_virtual_path(new_raw)
            if new_scope != SCOPE_AGENT:
                return _err(_NOT_SHARED_PATH)
            dst_rel = memory_file.validate_rel(new_rel, mutating=True)
        settings = await asyncio.to_thread(memory_store.get_settings)
        result = await memory_promote.promote(
            agent, username, rel, role=role, user_sub=sub, dst_rel=dst_rel,
            caps=memory_file.Caps.from_settings(settings),
        )
    except MemoryRoleError:
        raise HTTPException(403, _NOT_YOUR_TIER) from None
    except MemoryCapError as e:
        _log_outcome(agent, SCOPE_AGENT, "promote", rel, refusal=e)
        return _err(str(e))
    except MemoryOpError as e:
        return _err(str(e))
    _log_outcome(agent, SCOPE_AGENT, "promote", dst_rel, result=result)
    return _ok(result)


# ---------------------------------------------------------------------------
# Platform settings (admin only) + per-agent toggles (manager+)
# ---------------------------------------------------------------------------

@router.get("/settings")
async def get_settings_endpoint(
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not u.is_admin:
        raise HTTPException(403, "Only an admin can see the memory settings.")
    return await asyncio.to_thread(memory_store.get_settings)


@router.patch("/settings")
async def patch_settings_endpoint(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not u.is_admin:
        raise HTTPException(403, "Only an admin can change the memory settings.")
    body = await request.json()
    for key, (low, high) in _SETTING_RANGES.items():
        value = body.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise HTTPException(400, _SETTING_REFUSALS[key])
    return await asyncio.to_thread(memory_store.update_settings, **body)


@router.get("/agent-settings/{agent}")
async def get_agent_settings(
    agent: str,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "Only a manager of this agent can see its memory settings.")
    toggles = await asyncio.to_thread(memory_store.get_agent_toggles, agent)
    # The platform master switches ride along under a distinct key (the
    # per-agent toggles reuse the same field names) so agent managers can
    # grey out the UI without the admin-only GET /settings endpoint.
    settings = await asyncio.to_thread(memory_store.get_settings)
    toggles["master"] = {
        "user_memory_enabled": settings.get("user_memory_enabled", True),
        "agent_memory_enabled": settings.get("agent_memory_enabled", True),
    }
    return toggles


@router.patch("/agent-settings/{agent}")
async def patch_agent_settings(
    agent: str,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "Only a manager of this agent can change its memory settings.")
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        raise HTTPException(400, _NO_SUCH_SETTING)
    try:
        return await asyncio.to_thread(
            memory_store.set_agent_toggle, agent, key, value,
        )
    except ValueError as e:
        logger.warning("PATCH /v1/internal/memory/agent-settings/%s: %s", agent, e)
        raise HTTPException(400, _NO_SUCH_SETTING) from e


# ---------------------------------------------------------------------------
# Danger zone — clear all + per-user self clear
# ---------------------------------------------------------------------------

def _clear_memory_dir(agent_slug: str, scope: str, username: str | None) -> int:
    """Delete every file in one scope's memory dir (git-committed). Returns
    the number of files removed. Tombstones are recorded by the caller."""
    agent_dir = config.get_agent_dir(agent_slug)
    root = memory_file.scope_root(agent_dir, scope, username)
    if not root.is_dir():
        return 0
    files = [
        p for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and p.name != memory_file.LOCK_FILENAME
    ]
    if not files:
        return 0
    shutil.rmtree(root)
    try:
        from services.infra import git_writer
        repo = memory_file.git_repo_root(agent_dir, scope, username)
        if (repo / ".git").exists():
            git_writer.commit_paths(
                repo, [root], f"memory: clear ({scope} scope)",
            )
    except (ImportError, OSError, ValueError) as exc:
        logger.debug("Git commit of memory clear (%s scope) failed: %s", scope, exc)
    return len(files)


async def _clear_and_tombstone(
    agent_slug: str, scope: str, username: str | None,
) -> int:
    """Clear one scope dir + record per-file tombstones / fan out deletes so
    idle satellites apply the wipe instead of resurrecting it."""
    agent_dir = config.get_agent_dir(agent_slug)
    root = memory_file.scope_root(agent_dir, scope, username)
    rels: list[str] = []
    if root.is_dir():
        rels = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not p.is_symlink()
            and p.name != memory_file.LOCK_FILENAME
        ]
    count = await asyncio.to_thread(
        _clear_memory_dir, agent_slug, scope, username,
    )
    if rels:
        fake = OpResult(output="", deleted=rels)
        await memory_publish.publish_changes(
            agent_slug, agent_dir, root, fake, writer_slug=username,
        )
    return count


@router.post("/clear-all")
async def clear_all_endpoint(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Wipe memory for a scope across all (or one) agent. Admin only."""
    u = require_auth(user)
    if not u.is_admin:
        raise HTTPException(403, "Only an admin can clear everyone's memory.")
    body = await request.json()
    scope = body.get("scope")
    agent = body.get("agent")
    if scope not in ("user", "agent"):
        raise HTTPException(400, "Choose whose memory to clear.")

    if agent and not agent_store.agent_exists(agent):
        raise HTTPException(404, _AGENT_GONE)
    slugs = [agent] if agent else [a["slug"] for a in agent_store.get_all_agents()]
    files_unlinked = 0
    agents_affected = 0
    for slug in slugs:
        agent_dir = config.get_agent_dir(slug)
        if not agent_dir.exists():
            continue
        touched = 0
        if scope == "agent":
            touched += await _clear_and_tombstone(slug, "agent", None)
        else:
            users_dir = agent_dir / layout.USERS
            if users_dir.exists():
                for user_dir in users_dir.iterdir():
                    if user_dir.is_dir():
                        touched += await _clear_and_tombstone(
                            slug, "user", user_dir.name,
                        )
        files_unlinked += touched
        if touched:
            agents_affected += 1
    return {
        "files_unlinked": files_unlinked,
        "agents_affected": agents_affected,
        "scope": scope,
        "agent": agent,
    }


@router.post("/clear-agent-memory/{agent}")
async def clear_agent_memory(
    agent: str,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Manager+ (or admin): wipe ONE agent's SHARED memory scope
    (``knowledge/memory/``). Users' personal memories are untouched —
    those stay self-service (``clear-my-memory``) or admin (``clear-all``).
    """
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "Only a manager of this agent can clear its memory.")
    if not agent_store.agent_exists(agent):
        raise HTTPException(404, _AGENT_GONE)
    files_unlinked = await _clear_and_tombstone(agent, "agent", None)
    return {"files_unlinked": files_unlinked, "agent": agent, "scope": "agent"}


@router.post("/clear-my-memory")
async def clear_my_memory(
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """User self-service: wipe own user-scope memory across all agents."""
    u = require_auth(user)
    if not u.sub or u.sub.startswith("session:") or u.sub == "api-key":
        raise HTTPException(
            400, "Only a person signed in to the dashboard can clear their own memory.",
        )
    username = await asyncio.to_thread(task_store.get_username_by_sub, u.sub)
    if not username:
        logger.warning("POST /v1/internal/memory/clear-my-memory: no username for %s", u.sub)
        raise HTTPException(400, "Could not find your account. Sign out, sign in again and retry.")

    files_unlinked = 0
    agents_affected = 0
    for a in agent_store.get_all_agents():
        touched = await _clear_and_tombstone(a["slug"], "user", username)
        files_unlinked += touched
        if touched:
            agents_affected += 1
    return {"files_unlinked": files_unlinked, "agents_affected": agents_affected}
