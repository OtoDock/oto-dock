"""Chat REST API for dashboard chat persistence.

CRUD endpoints for chats and messages, authenticated via JWT session cookie.
"""

import asyncio
import logging
import re
import unicodedata
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from storage import database as task_store
from auth.providers import UserContext, get_current_user, require_auth, require_agent_access

logger = logging.getLogger("claude-proxy.chat-api")
router = APIRouter()

# Manual-rename titles fan out to OTHER users' sidebars (title_updated), so
# sanitization is a spoofing defense, not cosmetics: strip C0/C1 controls,
# bidi embeddings/overrides/isolates, zero-width + word-joiner + BOM. ZWNJ/ZWJ
# (200c/200d) are deliberately KEPT — composite emoji and Persian/Indic text
# need them. Whitespace is collapsed separately (before this strip, so a
# newline separates words instead of gluing them).
_TITLE_STRIP_RE = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u200b\u200e\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
_TITLE_MAX_LEN = 160  # code points, matching the client input's maxLength


# --- Request models ---


class CreateChatRequest(BaseModel):
    agent: str
    permission_mode: str = "default"


class UpdateChatRequest(BaseModel):
    title: str | None = None


# --- Endpoints ---


def _sanitize_title(raw: str) -> str:
    """Normalize + defang a manual rename: NFC, collapse whitespace, strip
    the hostile invisibles (``_TITLE_STRIP_RE``), re-collapse, clip. Returns
    '' for a title that was nothing but whitespace/invisibles — the handler
    rejects that with a 400."""
    t = unicodedata.normalize("NFC", raw or "")
    t = " ".join(t.split())
    t = _TITLE_STRIP_RE.sub("", t)
    t = " ".join(t.split())
    return t[:_TITLE_MAX_LEN].rstrip()


def _run_for_chat(chat: dict) -> dict | None:
    """The task run governing this chat's task-history permissions, or None
    for a plain chat. Keys on run-row EXISTENCE, never the id prefix — a
    chat-surface delegate worker has a plain-uuid chat id WITH a run row.
    Falls back to deriving the run id from a ``task-`` id for the create→
    stamp window (run row exists, ``chat_id`` not yet pointing at it)."""
    chat_id = chat.get("id", "")
    run = task_store.get_run_for_chat(chat_id)
    if run is None and chat_id.startswith("task-"):
        run = task_store.get_run(chat_id.removeprefix("task-"))
    return run


def _task_mutation_allowed(role: str, acting: str, scope: str,
                           created_by: str) -> bool:
    """The task mutation matrix (mirrors ``api/tasks``' ``_check_task_permission``):
    user-scope → creator only; agent-scope → admin/manager any, editor only
    their own, viewer never."""
    if role == "admin":
        return True
    if (scope or "agent") == "user":
        return bool(acting) and created_by == acting
    if role == "manager":
        return True
    if role == "editor":
        return bool(acting) and created_by == acting
    return False


def can_access_chat(u: UserContext, chat: dict) -> bool:
    """May this user view this dashboard chat?

    Owner-equality (per-user chats) OR admin OR — for a Shared-only agent's
    synthetic-owner chat — any user assigned to the agent OR — for a task-run
    chat (synthetic ``task::`` owner) — the task listing's run rule:
    agent-scoped runs for anyone with agent access, user-scoped runs only for
    their creator. Without that branch the REST surface denies rows the
    ``kind=tasks`` listing itself shows. Mutations layer ``can_mutate_chat``
    on top.
    """
    from core.session.visibility import is_shared_chat_owner
    if u.is_admin:
        return True
    owner = chat.get("user_sub", "")
    if owner == u.sub:
        return True
    if is_shared_chat_owner(owner):
        return u.can_access_agent(chat.get("agent", ""))
    if owner.startswith("task::") or chat.get("id", "").startswith("task-"):
        run = _run_for_chat(chat)
        if run is not None:
            if (run.get("scope") or "agent") == "user":
                return run.get("created_by") == (u.acting_sub or "")
            return u.can_access_agent(chat.get("agent", ""))
    return False


def can_mutate_chat(u: UserContext, chat: dict, run: dict | None = None,
                    *, run_resolved: bool = False) -> bool:
    """May this user RENAME or DELETE this chat? Stricter than view:

    - admin — any chat.
    - Task-history chats (a ``task_runs`` row exists: ``task-`` ids AND
      chat-surface delegate workers with uuid ids) — the task matrix
      (``_task_mutation_allowed``) judged on the RUN row.
    - Shared-only chats (synthetic ``agent::`` owner, no run row) — editor+.
      Viewers keep read access but no longer mutate shared history.
    - Everything else (per-user chats; phone's synthetic owner) — owner only.

    ``run``/``run_resolved`` let listing endpoints pass a batch-resolved run
    (or its absence) so the flags they stamp use EXACTLY this logic without a
    per-row query."""
    from core.session.visibility import is_shared_chat_owner
    if u.is_admin:
        return True
    if not run_resolved:
        run = _run_for_chat(chat)
    if run is not None:
        return _task_mutation_allowed(
            u.get_agent_role(chat.get("agent", "")), u.acting_sub or "",
            run.get("scope") or "agent", run.get("created_by") or "",
        )
    owner = chat.get("user_sub", "")
    if is_shared_chat_owner(owner):
        return u.can_edit_agent(chat.get("agent", ""))
    return owner == u.sub


def _task_scope_sub(u: UserContext) -> str | None:
    """The user-scope filter for task-mode listings — the user-view rule of
    ``/v1/tasks/runs`` (``_scope_filter_sub`` without audit): agent-scoped runs
    for anyone with agent access, user-scoped runs only for their creator.
    Only the master key skips filtering; admins get the user-view here too
    (the admin Task History page is the audit surface)."""
    return None if u.is_service else (u.acting_sub or "")


@router.get("/v1/chats")
async def list_chats(
    agent: str | None = Query(None),
    kind: str = Query("chats", pattern="^(chats|tasks)$"),
    limit: int = Query(50, ge=1, le=200),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    # kind=tasks → the sidebar's task mode: the agent's task-run chats joined
    # with their latest run, gated by the run rules (see _task_scope_sub).
    if kind == "tasks":
        if not agent:
            raise HTTPException(status_code=400, detail="agent parameter required")
        require_agent_access(u, agent)
        chats = task_store.list_task_chats(agent, _task_scope_sub(u), limit=limit)
        _stamp_mutation_flags(u, chats)
        return {"chats": chats}
    # Shared-only agents have ONE shared chat list (synthetic owner); every
    # other mode lists the user's own. A global (no-agent) list stays per-user.
    from core.session.visibility import chat_history_owner
    owner = chat_history_owner(agent, u.sub) if agent else u.sub
    chats = task_store.list_chats(owner, agent=agent, limit=limit)
    _stamp_mutation_flags(u, chats)
    return {"chats": chats}


def _stamp_mutation_flags(u: UserContext, rows: list[dict]) -> None:
    """Server-computed per-row ``can_rename``/``can_delete`` — the client
    renders menu options from these and NEVER re-implements the role matrix,
    so the flags must judge exactly what the mutation endpoints will.

    Task-listing rows carry their run/definition columns inline
    (``_TASK_RUN_COLS``); plain chat rows batch-resolve runs in ONE query
    (uuid delegate-worker chats live in plain lists). ``can_delete`` always
    judges the RUN (delete removes the chat's history entry); ``can_rename``
    judges what rename actually hits — the DYNAMIC TASK row for name-labeled
    task rows (that rename goes to ``PATCH /v1/tasks``, judged on the
    definition's scope/creator), else the chat/run."""
    if not rows:
        return
    need_lookup = [r.get("id", "") for r in rows if "run_id" not in r]
    runs_by_chat = task_store.get_runs_for_chats(need_lookup) if need_lookup else {}
    for r in rows:
        if "run_id" in r:
            run = ({"scope": r.get("run_scope"),
                    "created_by": r.get("run_created_by")}
                   if r.get("run_id") else None)
        else:
            run = runs_by_chat.get(r.get("id", ""))
        r["can_delete"] = can_mutate_chat(u, r, run, run_resolved=True)
        if r.get("task_name") and r.get("task_id"):
            r["can_rename"] = _task_mutation_allowed(
                u.get_agent_role(r.get("agent", "")), u.acting_sub or "",
                r.get("task_scope") or "agent", r.get("task_created_by") or "",
            )
        else:
            r["can_rename"] = r["can_delete"]


def _widget_shows_agent(u: UserContext, agent: str) -> bool:
    """Assignment-scoped agent gate for the Active-now widget — deliberately
    NOT ``can_access_agent`` (admin bypass): an admin CAN open any agent's
    chats, but the widget ADVERTISES work and must not surface agents the
    viewer never added to their list (live-observed 2026-07-11: the sample
    agents' unread chats on an admin's widget). Mirrors the ``chat_status``
    WS fan-out, which already scopes to the agent's assigned users."""
    return agent in (u.agents or ()) or \
        (u.is_session and bool(u.agent) and agent == u.agent)


def _widget_chat_visible(u: UserContext, chat: dict) -> bool:
    """May this chat appear in the viewer's Active-now widget? Own chats
    always; shared-only chats when the agent is on the viewer's list. Other
    users' personal chats never — even for admins (``can_access_chat`` still
    governs actually OPENING them; the widget is discovery, not audit)."""
    from core.session.visibility import is_shared_chat_owner
    owner = chat.get("user_sub", "")
    if owner == u.sub:
        return True
    return is_shared_chat_owner(owner) and _widget_shows_agent(u, chat.get("agent", ""))


@router.get("/v1/chats/active")
async def list_active_chats(
    user: UserContext | None = Depends(get_current_user),
):
    """Chats with an OPEN TURN right now, across every agent this user may see.

    The seed for the sidebar's cross-agent "Active now" widget: the client
    keeps rows live from the ``chat_status`` WS broadcasts it already
    receives; this endpoint only supplies the metadata (title/agent) those
    broadcasts don't carry. Composition mirrors the WS connect snapshot
    (``ws/dashboard.py`` chat_status_snapshot): the union of pump turns and
    interactive PTY turns — both in-memory sets, so this is cheap (one
    ``get_chat`` per active id, no table scans). Per-row visibility is
    ASSIGNMENT-scoped (``_widget_chat_visible``): own chats + shared chats of
    agents on the viewer's list — deliberately narrower than
    ``can_access_chat``'s admin bypass, matching the ``chat_status`` WS
    fan-out (the widget advertises work; the admin audit pages are the
    full-view surfaces). NOTE: declared before ``/v1/chats/{chat_id}`` so the
    literal path isn't captured by the param route.
    """
    u = require_auth(user)
    from core.session.session_state import streaming_chat_ids as pump_streaming
    from core.session import interactive_session
    from core.session.visibility import is_shared_chat_owner

    rows: list[dict] = []
    seen: set[str] = set()
    for cid in list(pump_streaming()) + list(interactive_session.streaming_chat_ids()):
        if not cid or cid in seen:
            continue
        seen.add(cid)
        chat = task_store.get_chat(cid)
        if not chat:
            continue
        title = chat.get("title") or ""
        if cid.startswith("task-run-"):
            # Task rows click through to the per-agent Task History, which
            # scopes runs like /v1/tasks/runs (audit=false): agent-scoped runs
            # for anyone with agent access, user-scoped runs only for their
            # creator — deliberately NO admin bypass (the admin audit page is
            # the full-view surface). Gate the row on the RUN's visibility so
            # the widget never emits a row whose destination page is empty.
            run = task_store.get_run(cid.removeprefix("task-"))
            if not run or not _widget_shows_agent(u, run.get("agent", "")):
                continue
            if (run.get("scope") or "agent") == "user" and \
                    run.get("created_by") != u.acting_sub:
                continue
            # Task rows are labeled by the task's NAME, matching the sidebar's
            # task mode; the chat title (prompt first line / LLM upgrade) is
            # the fallback for runs whose task row is already gone.
            dyn = task_store.get_dynamic_task(run.get("task_id") or "")
            if dyn and dyn.get("name"):
                title = dyn["name"]
        elif not _widget_chat_visible(u, chat):
            continue
        rows.append({
            "id": cid,
            "agent": chat.get("agent", ""),
            "title": title,
            "status": "streaming",
            # Task-run chats are created with the DEFAULT source_type
            # ('chat') — their durable marker is the id prefix. The widget
            # renders task rows purple and routes them to the run view,
            # so report them as what they are.
            "source_type": ("task" if cid.startswith("task-run-")
                            else chat.get("source_type") or ""),
            "owner_is_shared": is_shared_chat_owner(chat.get("user_sub", "")),
            "last_response_at": chat.get("last_response_at"),
        })

    # Finished-unread backfill: recent responses nobody opened yet stay in the
    # widget across page reloads (status 'finished' — the client renders the
    # steady done tint + dot and retires the row on open). Same per-row access
    # rule as the streaming set; 48h window, so an abandoned chat eventually
    # ages out of the widget while staying unread in its own history list.
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    for chat in task_store.list_unread_finished_chats(since):
        cid = chat.get("id", "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        if not _widget_chat_visible(u, chat):
            continue
        rows.append({
            "id": cid,
            "agent": chat.get("agent", ""),
            "title": chat.get("title") or "",
            "status": "finished",
            "source_type": chat.get("source_type") or "",
            "owner_is_shared": is_shared_chat_owner(chat.get("user_sub", "")),
            "last_response_at": chat.get("last_response_at"),
            "unread": True,
        })
    return {"chats": rows}


@router.get("/v1/chats/search")
async def search_chats(
    q: str = Query(..., min_length=1),
    agent: str | None = Query(None),
    kind: str = Query("chats", pattern="^(chats|tasks)$"),
    limit: int = Query(20, ge=1, le=100),
    user: UserContext | None = Depends(get_current_user),
):
    """FTS over chat titles + content. Search follows the sidebar mode:
    kind=chats scopes to the viewer's history owner and excludes task-run
    chats; kind=tasks searches the agent's task-run chats under the run
    permission rules (same gating as the kind=tasks listing)."""
    u = require_auth(user)
    if not agent:
        raise HTTPException(status_code=400, detail="agent parameter required")
    if kind == "tasks":
        require_agent_access(u, agent)
        results = task_store.search_task_chats(agent, q, _task_scope_sub(u), limit=limit)
        _stamp_mutation_flags(u, results)
        return {"chats": results}
    from core.session.visibility import chat_history_owner
    results = task_store.search_chats(chat_history_owner(agent, u.sub), agent, q, limit=limit)
    _stamp_mutation_flags(u, results)
    return {"chats": results}


@router.get("/v1/chats/{chat_id}")
async def get_chat(
    chat_id: str,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")
    # One paged logic for both the initial window and lazy scroll-back: the newest
    # `limit` rows (older than `before_id` when scrolling back) + whether still-older
    # rows remain. An older page needs no `chat`; the initial fetch carries it.
    messages, has_more = task_store.get_chat_messages_page(chat_id, limit, before_id=before_id)
    if before_id is not None:
        return {"messages": messages, "has_more": has_more}
    return {"chat": chat, "messages": messages, "has_more": has_more}


@router.get("/v1/chats/{chat_id}/project")
async def get_chat_project(
    chat_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The delegation project this chat participates in: the sibling lanes
    (orchestrator + workers, any agent) with their LIVE lane status. Anchor
    authz = access to the anchor chat; each sibling row is then filtered by
    the same per-chat rule, so a project spanning agents never leaks lanes the
    viewer couldn't open. The board file itself is read client-side through
    the workspace files API — this endpoint only supplies the lane graph.

    Every delegation gets the dock, project slug or not: without a
    ``project_id`` the graph falls back to LINEAGE — the anchor's root chat
    (its parent, or itself when it is the delegating chat) plus the workers
    that root spawned. 404 only for chats with no delegation markers at all."""
    u = require_auth(user)
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")
    project_id = chat.get("project_id") or ""
    if project_id:
        rows = task_store.list_chats_by_project(project_id)
    elif chat.get("parent_chat_id") or chat.get("delegate_role") \
            or chat.get("origin") == "delegated":
        root_id = chat.get("parent_chat_id") or chat_id
        root = chat if root_id == chat_id else task_store.get_chat(root_id)
        rows = ([root] if root else []) + task_store.list_chats_by_parent(root_id)
    else:
        raise HTTPException(status_code=404, detail="Chat has no project")
    from services.delegation.lane_status import chat_status
    lanes = [
        {
            "id": r["id"],
            "title": r.get("title") or "",
            "agent": r.get("agent", ""),
            "delegate_role": r.get("delegate_role") or "",
            # Lineage lets the client scope the live cards to the anchor's own
            # delegation round — a reused project slug spans many rounds.
            "parent_chat_id": r.get("parent_chat_id") or "",
            "status": chat_status(r["id"]),
            "updated_at": r.get("updated_at"),
        }
        for r in rows
        if can_access_chat(u, r)
    ]
    return {"project_id": project_id, "chats": lanes}


@router.get("/v1/chats/{chat_id}/pins")
async def get_chat_pins(
    chat_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The chat's Dock pins: its own chat-scoped mini-app (if any) and — when
    the chat belongs to a delegation project — the project-scoped one. Anchor
    authz = access to the chat (same rule as opening it); each pin row is then
    filtered by the app's OWN serve rule (``app_access``: personal rows →
    owner only), so a personal-scope project pin never leaks to other viewers
    — the documented v1 limit. Rows are shaped exactly like ``GET /v1/apps``
    (id/actions/sig/approval) so the overlay reuses AppFrame + the approval
    card unchanged. Soft-hidden pins are absent, like every viewer surface.

    ``files`` carries the Dock FILE pins (chat scope + project scope, in pin
    order) — just references: the overlay reads the content through the
    files API, where the viewer's own path-policy role decides what renders
    (a pin row itself leaks only the path + title)."""
    u = require_auth(user)
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")
    from api.apps.apps import app_access, shape_app_rows

    def _load() -> dict:
        out: dict = {"chat": None, "project": None, "files": []}
        project_id = chat.get("project_id") or ""
        pairs = [("chat", task_store.get_scoped_app(chat_id=chat_id))]
        if project_id:
            pairs.append(
                ("project", task_store.get_scoped_app(project_id=project_id)))
        for key, row in pairs:
            if not row or row.get("hidden") or not app_access(row, u):
                continue
            shaped = shape_app_rows([row], u)[0]
            shaped["agent"] = row.get("agent") or ""
            out[key] = shaped
        file_rows = task_store.list_file_pins(chat_id=chat_id)
        if project_id:
            file_rows += task_store.list_file_pins(project_id=project_id)
        out["files"] = [{
            "id": r["id"],
            "agent": r["agent"],
            "rel_path": r["rel_path"],
            "title": r["title"],
            "pin_scope": "chat" if r.get("scope_chat_id") else "project",
            "updated_at": r["updated_at"],
        } for r in file_rows]
        return out

    return await asyncio.to_thread(_load)


@router.post("/v1/chats")
async def create_chat(
    req: CreateChatRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    require_agent_access(u, req.agent)
    chat_id = str(uuid.uuid4())
    # Shared-only agents collapse into ONE shared chat list (synthetic owner).
    from core.session.visibility import chat_history_owner
    chat = task_store.create_chat(
        chat_id, chat_history_owner(req.agent, u.sub), req.agent, req.permission_mode,
    )
    return {"chat": chat}


@router.patch("/v1/chats/{chat_id}")
async def update_chat(
    chat_id: str,
    req: UpdateChatRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat) or not can_mutate_chat(u, chat):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to rename this chat")
    updates: dict = {}
    if req.title is not None:
        title = _sanitize_title(req.title)
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        updates["title"] = title
        # A manual rename finalizes the title — mark it generated so the one-time
        # LLM title upgrade (services/title_generator.py) never overwrites it.
        updates["title_generated"] = True
    if updates:
        task_store.update_chat(chat_id, **updates)
        if "title" in updates:
            # Fan out to every viewer's sidebar + Active-now widget NOW — a
            # rename must not wait for the next navigation refetch. Pass the
            # ROW's owner (chat_status_targets resolves agent::/task:: owners
            # to the agent's assigned users).
            try:
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_title(
                    chat.get("user_sub") or "", chat_id, updates["title"],
                    agent=chat.get("agent") or "",
                )
            except Exception:
                logger.debug("rename broadcast failed for %s", chat_id,
                             exc_info=True)
    # Echo the sanitized title so the client renders what was stored.
    return {"status": "ok", "title": updates.get("title")}


async def _close_chat_session(chat: dict) -> None:
    """Close a deleted chat's live session on whichever layer holds it.

    Without this, deleting a chat leaves its warm CLI/daemon running headless
    (holding a subscription seat + satellite/local slot) until the idle
    reaper — or longer, when background work keeps extending the reaper's
    leash. Registry membership, not resolution, picks the layer: the chat's
    stored execution_path may be stale relative to where the session actually
    lives. Best-effort — an absent/dead session is fine."""
    session_id = chat.get("session_id") or ""
    agent = chat.get("agent") or ""
    if not session_id or not agent:
        return
    try:
        from core.session.session_manager import (
            get_execution_layer, _remote_layer,
        )
        from core.layers.cli.session import _persistent_sessions
        from core.layers.codex.session import _codex_sessions
        from core.layers.direct.session import _direct_sessions
        if _remote_layer and session_id in _remote_layer._sessions:
            layer = _remote_layer
        elif session_id in _persistent_sessions:
            layer = get_execution_layer(agent, execution_path="claude-code-cli",
                                        execution_target="local")
        elif session_id in _codex_sessions:
            layer = get_execution_layer(agent, execution_path="codex-cli",
                                        execution_target="local")
        elif session_id in _direct_sessions:
            layer = get_execution_layer(agent, execution_path="direct-llm",
                                        execution_target="local")
        else:
            return
        await layer.close_session(session_id)
    except Exception:
        logger.warning("Chat delete: closing live session %s failed",
                       session_id[:8], exc_info=True)


@router.delete("/v1/chats/{chat_id}")
async def delete_chat(
    chat_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat) or not can_mutate_chat(u, chat):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to delete this chat")
    # A live/queued run would keep writing rows (and holding a session)
    # against a deleted chat — deliberately an EXISTS over ALL the chat's
    # runs, never "the latest run's status" (a pending run has no started_at
    # and sorts last on a multi-run worker chat).
    if await asyncio.to_thread(task_store.has_live_run, chat_id):
        raise HTTPException(
            status_code=409,
            detail="A task run is still active on this chat — cancel it "
                   "first, then delete the history entry.")
    # A deleted chat must never be woken — cancel its pending continuations
    # (row + APScheduler job) before the row goes.
    from services.scheduler import scheduler
    for cont in task_store.list_continuations_for_chat(chat_id):
        await scheduler.remove_dynamic_task(cont["id"])
    await _close_chat_session(chat)
    task_store.delete_chat(chat_id)
    from services.media import preview_snapshots
    await asyncio.to_thread(preview_snapshots.delete_chat_dir, chat_id)
    return {"status": "ok"}


@router.patch("/v1/chats/{chat_id}/dismiss-preview/{file_id}")
async def dismiss_preview(
    chat_id: str,
    file_id: str,
    snapshot_id: str | None = None,
    message_id: int | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """Dismiss document_preview events for a file_id in a chat. Persisted.

    ``snapshot_id`` / ``message_id`` scope the dismissal to one preview
    instance (a "previous version" block closing itself); with neither, every
    instance for the file is dismissed (the live block's close)."""
    u = require_auth(user)
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")
    count, freed = task_store.dismiss_document_previews(
        chat_id, file_id, snapshot_id=snapshot_id, db_message_id=message_id,
    )
    if freed:
        from services.media import preview_snapshots

        def _drop() -> None:
            for sid in freed:
                preview_snapshots.delete_snapshot(chat_id, sid)
        await asyncio.to_thread(_drop)
    return {"status": "ok", "dismissed": count}
