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
     writes reach open satellites and Files-UI views mid-session.

Settings endpoints (admin / manager, dashboard cookies) and the danger-zone
``clear-all`` / ``clear-my-memory`` remain at the bottom.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

import config
from auth.providers import UserContext, get_current_user, require_auth
from storage import agent_store
from storage import database as task_store
from storage import memory_store
from services.memory import memory_file
from services.memory.memory_file import MemoryOpError, OpResult


logger = logging.getLogger("claude-proxy.memory-api")
router = APIRouter(prefix="/v1/internal/memory")


_COMMANDS = {"view", "create", "str_replace", "insert", "delete", "rename"}
_WRITE_COMMANDS = {"create", "str_replace", "insert", "delete", "rename"}


# ---------------------------------------------------------------------------
# Role + toggle helpers
# ---------------------------------------------------------------------------

def _resolve_effective_role(user_sub: str, agent: str) -> str:
    """Real role at this (user, agent) pair, ignoring ``is_api_key`` inflation.

    Returns ``"admin"`` / ``"manager"`` / ``"editor"`` / ``"viewer"``, or
    ``"agent"`` for agent-scope service sessions with no user owner.
    """
    if not user_sub:
        return "agent"
    u = task_store.get_user(user_sub)
    if u and u.get("role") == "admin":
        return "admin"
    roles = task_store.get_user_agent_roles(user_sub)
    return roles.get(agent, "viewer")


def _require_mcp_caller(user: UserContext, agent: str) -> None:
    """Memory-mcp callback endpoints accept only session-JWT auth."""
    if not user.is_api_key:
        raise HTTPException(403, "memory endpoint requires session API key")
    if not agent:
        raise HTTPException(400, "X-Agent-Name required")


def _scope_enabled(scope: str, agent: str) -> bool:
    """Master AND per-agent toggle compose."""
    settings = memory_store.get_settings()
    toggles = memory_store.get_agent_toggles(agent)
    if scope == "agent":
        return bool(
            settings.get("agent_memory_enabled")
            and toggles.get("agent_memory_enabled")
        )
    if scope == "user":
        return bool(
            settings.get("user_memory_enabled")
            and toggles.get("user_memory_enabled")
        )
    return False


def _err(output: str, warnings: list[str] | None = None) -> dict[str, Any]:
    return {"output": output, "is_error": True, "warnings": warnings or []}


def _ok(result: OpResult) -> dict[str, Any]:
    return {
        "output": result.output,
        "is_error": False,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# Write propagation (mirrors the dashboard file API's publish pipeline)
# ---------------------------------------------------------------------------

def _tree_rel(agent_dir: Path, root: Path, rel: str) -> str | None:
    """Agent-tree-relative posix path for a scope-relative memory path."""
    try:
        return (root / rel).resolve().relative_to(agent_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return None


async def _publish_changes(
    agent_slug: str,
    agent_dir: Path,
    root: Path,
    result: OpResult,
    *,
    writer_slug: str | None,
    exclude_user_sub: str = "",
) -> None:
    """Bookkeeping + live propagation for a successful mutation. Best-effort —
    a satellite being offline or a broadcast failing never fails the op."""
    try:
        from services.remote import workspace_fanout
        from services.notifications.notification_manager import broadcast_file_updated
        from storage import file_author_store, file_tombstones_store

        for rel in result.changed:
            tree_rel = _tree_rel(agent_dir, root, rel)
            if not tree_rel:
                continue
            await asyncio.to_thread(
                file_tombstones_store.drop, agent_slug, tree_rel,
            )
            if writer_slug:
                await asyncio.to_thread(
                    file_author_store.record, agent_slug, tree_rel, writer_slug,
                )
            if workspace_fanout.has_fanout_candidates(
                agent_slug, tree_rel, include_idle=True,
            ):
                try:
                    content = (root / rel).read_bytes()
                except OSError:
                    content = None
                if content is not None:
                    await workspace_fanout.fan_out_write(
                        agent_slug, tree_rel, content, include_idle=True,
                    )
            await broadcast_file_updated(
                agent_slug, tree_rel, exclude_user_sub=exclude_user_sub,
            )

        now = time.time()
        for rel in result.deleted:
            tree_rel = _tree_rel(agent_dir, root, rel)
            if not tree_rel:
                continue
            await asyncio.to_thread(
                file_tombstones_store.record, agent_slug, tree_rel, now,
                origin="memory",
            )
            await asyncio.to_thread(
                file_author_store.clear, agent_slug, tree_rel,
            )
            await workspace_fanout.fan_out_delete(
                agent_slug, tree_rel, include_idle=True,
            )
            await broadcast_file_updated(
                agent_slug, tree_rel, exclude_user_sub=exclude_user_sub,
            )
    except Exception:
        logger.warning(
            "memory write propagation failed for %s", agent_slug, exc_info=True,
        )


def _commit(
    agent_dir: Path, scope: str, username: str | None,
    result: OpResult, command: str, writer: str,
) -> str | None:
    """One attributed git commit covering every path the op touched."""
    try:
        from services.infra import git_writer
        repo = memory_file.git_repo_root(agent_dir, scope, username)
        repo.mkdir(parents=True, exist_ok=True)
        git_writer.init_if_missing(repo)
        root = memory_file.scope_root(agent_dir, scope, username)
        paths = [root / rel for rel in (result.changed + result.deleted)]
        if not paths:
            return None
        return git_writer.commit_paths(
            repo, paths, f"memory: {command} ({scope} scope, by {writer})",
        )
    except Exception:
        logger.warning("memory git commit failed", exc_info=True)
        return None


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

    body = await request.json()
    command = body.get("command")
    if command not in _COMMANDS:
        raise HTTPException(
            400,
            f"unknown command: {command!r} (expected one of {sorted(_COMMANDS)})",
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
    scopes: dict[str, Path] = {}
    if _scope_enabled("agent", agent):
        scopes["agent"] = memory_file.scope_root(agent_dir, "agent")
    if username and _scope_enabled("user", agent):
        scopes["user"] = memory_file.scope_root(agent_dir, "user", username)
    if not scopes:
        return _err("Memory is disabled for this agent.")

    # rename carries two paths; both must land in the SAME scope.
    raw_path = body.get("old_path") if command == "rename" else body.get("path")
    if not raw_path or not isinstance(raw_path, str):
        raise HTTPException(400, "path required")

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
            if scope == "user" and not username:
                return _err(
                    "user-scope memory is not available in this session "
                    "(no user owner). Use /memories/agent/ instead."
                )
            return _err(f"The {scope} memory scope is disabled for this agent.")

        # Role gate: writes to agent scope need editor+ (or an agent-scope
        # service session); user scope is always the caller's own.
        if command in _WRITE_COMMANDS and scope == "agent":
            if role not in ("editor", "manager", "admin", "agent"):
                return _err(
                    "agent memory is read-only for viewers — save user-scope "
                    "memories under /memories/user/ instead."
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
            )
        elif command == "str_replace":
            result = await asyncio.to_thread(
                memory_file.op_str_replace, root, rel,
                body.get("old_str") or "", body.get("new_str") or "",
            )
        elif command == "insert":
            insert_line = body.get("insert_line")
            if not isinstance(insert_line, int):
                raise HTTPException(400, "insert_line (integer) required")
            result = await asyncio.to_thread(
                memory_file.op_insert, root, rel, insert_line,
                body.get("insert_text") or "",
            )
        elif command == "delete":
            result = await asyncio.to_thread(memory_file.op_delete, root, rel)
        else:  # rename
            new_raw = body.get("new_path")
            if not new_raw or not isinstance(new_raw, str):
                raise HTTPException(400, "new_path required")
            new_scope, new_rel = memory_file.split_virtual_path(new_raw)
            if new_scope != scope:
                return _err(
                    "Error: rename cannot move a file across memory scopes "
                    f"({scope} → {new_scope}). Create it in the target scope "
                    "and delete the original instead."
                )
            new_rel = memory_file.validate_rel(new_rel, mutating=True)
            result = await asyncio.to_thread(
                memory_file.op_rename, root, rel, new_rel,
            )
    except MemoryOpError as e:
        return _err(str(e))

    if result.changed or result.deleted:
        writer = username or "agent-session"
        await asyncio.to_thread(
            _commit, agent_dir, scope, username, result, command, writer,
        )
        await _publish_changes(
            agent, agent_dir, root, result,
            writer_slug=username, exclude_user_sub=user_sub,
        )
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
        raise HTTPException(403, "admin only")
    return await asyncio.to_thread(memory_store.get_settings)


@router.patch("/settings")
async def patch_settings_endpoint(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not u.is_admin:
        raise HTTPException(403, "admin only")
    body = await request.json()
    return await asyncio.to_thread(memory_store.update_settings, **body)


@router.get("/agent-settings/{agent}")
async def get_agent_settings(
    agent: str,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "manager or admin only")
    return await asyncio.to_thread(memory_store.get_agent_toggles, agent)


@router.patch("/agent-settings/{agent}")
async def patch_agent_settings(
    agent: str,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "manager or admin only")
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        raise HTTPException(400, "key required")
    try:
        return await asyncio.to_thread(
            memory_store.set_agent_toggle, agent, key, value,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    except Exception:
        pass
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
        await _publish_changes(
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
        raise HTTPException(403, "admin only")
    body = await request.json()
    scope = body.get("scope")
    agent = body.get("agent")
    if scope not in ("user", "agent"):
        raise HTTPException(400, "scope must be 'user' or 'agent'")

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
            users_dir = agent_dir / "users"
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
        raise HTTPException(403, "manager or admin only")
    if not agent_store.agent_exists(agent):
        raise HTTPException(404, f"agent not found: {agent}")
    files_unlinked = await _clear_and_tombstone(agent, "agent", None)
    return {"files_unlinked": files_unlinked, "agent": agent, "scope": "agent"}


@router.post("/clear-my-memory")
async def clear_my_memory(
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """User self-service: wipe own user-scope memory across all agents."""
    u = require_auth(user)
    if not u.sub or u.sub.startswith("session:") or u.sub == "api-key":
        raise HTTPException(400, "no user sub")
    username = await asyncio.to_thread(task_store.get_username_by_sub, u.sub)
    if not username:
        raise HTTPException(400, "username not found")

    files_unlinked = 0
    agents_affected = 0
    for a in agent_store.get_all_agents():
        touched = await _clear_and_tombstone(a["slug"], "user", username)
        files_unlinked += touched
        if touched:
            agents_affected += 1
    return {"files_unlinked": files_unlinked, "agents_affected": agents_affected}
