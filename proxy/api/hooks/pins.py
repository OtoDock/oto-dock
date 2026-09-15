"""Pins: mini-app pins (pin, unpin, list) and file pins from inside a session.

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from storage import database as task_store
from api.sessions.sessions import verify_session_match
from core.session.session_state import (
    get_session_security,
)
from api.hooks import artifacts, paths

logger = logging.getLogger("claude-proxy")
router = APIRouter()


class HookAppPinRequest(BaseModel):
    session_id: str
    slug: str
    title: str = ""       # empty on create → slug; empty on update → keep
    html: str = ""        # required on first pin; optional on update
    actions: list | None = None  # None = leave unchanged; [] = clear
    make_default: bool = False
    # "standing" (default, the apps strip) | "chat" | "project" — scoped pins
    # surface on the Dock overlay. Scope ids are SESSION-DERIVED only (the
    # session's chat / its project), never caller-supplied.
    scope: str = "standing"
    # OWNERSHIP (S1, orthogonal to ``scope``): "agent" = one shared row/file
    # for every user of the agent; "user" = the caller's personal row/file;
    # "" = the session's effective default scope (mode default with the
    # viewer clamp — the same value scope-aware MCPs see as
    # OTO_DEFAULT_SCOPE).
    visibility: str = ""


class HookAppSlugRequest(BaseModel):
    session_id: str
    slug: str = ""
    # Unpin only: "" = auto-detect the namespace (unambiguous slug), else
    # "user" | "agent" to disambiguate a slug pinned in both.
    visibility: str = ""


def _app_scope(ctx) -> tuple[str, str | None]:
    """(username, owner_sub) for the caller's mini-app scope. Keyed on the
    MOUNT identity: agent-scope sessions — service sessions AND Shared-only
    human chats (whose ctx.username stays set for attribution) — pin SHARED
    rows with owner_sub NULL (see schema.init_pinned_apps)."""
    mu = ctx.mount_username
    if not mu:
        return "", None
    return mu, task_store.get_user_sub_by_username(mu)


def _resolve_pin_visibility(ctx, requested: str) -> tuple[str, str | None]:
    """(username, owner_sub) for a pin's OWNERSHIP (S1).

    ``requested`` is the tool's ``visibility`` arg. Empty = the session's
    effective default scope — the same resolution ``resolve_visibility``
    feeds scope-aware MCPs (no user → agent; viewer → user; else the mode
    default, availability-clamped), so the tool schema's advertised default
    and the server's fallback can never disagree. Service sessions and
    Shared-only chats resolve to "agent" exactly as the old mount-derived
    rule did.

    Gates: the agent's mode must OFFER the scope (400 otherwise);
    "user" needs a real human identity on the session (400 for agent-scope
    tasks/triggers/phone); "agent" from a human additionally passes the H3
    editor+ gate at the call sites (``_require_shared_pin_authority``).
    """
    from core.session.visibility import available_scopes_for
    from storage.agents import agent_store as _agent_store
    row = _agent_store.get_agent(ctx.agent) or {}
    available = available_scopes_for(
        bool(row.get("collaborative", True)), row.get("default_scope") or "user",
    )
    vis = (requested or "").strip().lower()
    if vis and vis not in ("user", "agent"):
        raise HTTPException(status_code=400,
                            detail='visibility must be "user" or "agent"')
    if not vis:
        if not ctx.username or ctx.session_scope == "agent":
            # Service sessions AND Shared-only human chats (agent mount).
            # The MOUNT is ground truth here — it must win even over a
            # missing/odd agents row, so a shared-only chat can never
            # default into a per-user dir its mode doesn't have (the
            # 2026-07-10 live bug class).
            vis = "agent"
        elif ctx.role == "viewer" and "user" in available:
            vis = "user"
        else:
            vis = row.get("default_scope") or "user"
        if vis not in available:
            vis = available[0]
    elif vis not in available:
        raise HTTPException(
            status_code=400,
            detail=f"this agent's mode does not offer {vis!r}-visibility "
                   f"pins (mode offers: {', '.join(available)})",
        )
    if vis == "agent":
        return "", None
    if not ctx.username:
        raise HTTPException(
            status_code=400,
            detail='visibility="user" needs a session with a user — this '
                   "session has none (agent-scope task/trigger/service)",
        )
    return ctx.username, task_store.get_user_sub_by_username(ctx.username)


def _require_shared_pin_authority(ctx, username: str) -> None:
    """H3: a SHARED-target pin/unpin from a HUMAN chat needs editor+.

    Shared-only agents mount the agent scope for every role, and the proxy
    writes the app file itself (this hook is otherwise un-RBAC-gated) — so
    without this gate a VIEWER chatting with a shared-only agent could
    create, replace, or delete the TEAM-wide dashboard despite their
    read-only workspace mount. Service sessions (no human, ``ctx.username``
    empty) keep full authority: scheduled refresh tasks ARE the shared-
    dashboard update path. Mirrors the platform's viewer clamp ("viewers
    can never create agent-scope artifacts")."""
    if not username and ctx.username and ctx.role == "viewer":
        raise HTTPException(
            status_code=403,
            detail="viewer role cannot pin, update, or remove a shared "
                   "(team) dashboard — editor or manager required",
        )


@router.post("/v1/hooks/apps/pin")
async def hook_app_pin(req: HookAppPinRequest, authorization: str | None = Header(None)):
    """Called by display-mcp's pin_app to create/update a pinned mini-app.

    Upsert by (agent, caller scope, slug): the HTML (verbatim, wrapped at
    serve time like /v1/ui) lives at the FIXED path ``apps/<slug>.html``
    under the caller's scope workspace — no caller path input, deterministic
    ``file_updated`` matching, in-place update. Re-pinning with new html IS
    the live-refresh path for scheduled tasks (a native Write doesn't
    broadcast file_updated on local sandboxes). A changed actions manifest
    silently breaks the approval sig — buttons stay dead until the user
    re-approves (api/apps/manifest.py has the authority rules).

    Restore paths: pinning a slug the user soft-unpinned from the dashboard
    revives the hidden row (manifest + approval intact — the ack says so);
    with no row at all, html may still be omitted when ``apps/<slug>.html``
    already exists in the caller's scope (unpin keeps the file by design).

    ``scope="chat"|"project"`` pins a Dock dashboard instead of a standing
    app: the scope id resolves from THIS session's chat row (its id / its
    ``project_id``) — never from caller input, so there is nothing to forge.
    A scoped pin REPLACES the scope's existing pin (scope is the identity,
    slug is cosmetic); approval carries iff the manifest is unchanged.

    ``visibility="user"|"agent"`` (S1) picks the OWNERSHIP — personal row +
    ``users/<u>/workspace/apps/`` vs shared row + ``workspace/apps/`` —
    independent of ``scope``. Default = the session's effective default
    scope; see ``_resolve_pin_visibility`` for the gates. A slug may exist
    in BOTH namespaces (distinct rows + files); restore-by-slug looks up
    only the resolved namespace.
    """
    from api.apps import manifest as _mf

    verify_session_match(authorization, req.session_id)
    slug = req.slug.strip().lower()
    if not _mf.APP_SLUG_RE.match(slug):
        raise HTTPException(status_code=400,
                            detail="slug must be 1-40 chars of [a-z0-9-], starting alphanumeric")
    title = req.title.strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title exceeds 200 characters")
    if req.html and len(req.html.encode("utf-8", errors="ignore")) > artifacts._UI_MAX_HTML_BYTES:
        raise HTTPException(status_code=400, detail="html exceeds the 2MB artifact cap")
    scope = (req.scope or "standing").strip().lower()
    if scope not in ("standing", "chat", "project"):
        raise HTTPException(status_code=400,
                            detail='scope must be "standing", "chat" or "project"')

    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    agent_dir = config.get_agent_dir(ctx.agent)
    username, owner_sub = await asyncio.to_thread(
        _resolve_pin_visibility, ctx, req.visibility,
    )
    _require_shared_pin_authority(ctx, username)

    # Session-derived scope resolution (DECIDED: no caller-supplied ids —
    # a refresh task re-pinning a project app runs as a continuation of a
    # project chat and inherits the right ids from ITS chat).
    scope_chat_id, scope_project_id = "", ""
    if scope != "standing":
        chat = await asyncio.to_thread(
            task_store.get_chat_by_session, req.session_id,
        )
        if not chat:
            raise HTTPException(
                status_code=400,
                detail=f'scope="{scope}" needs a chat-bound session — this '
                       "session has no chat (pin from a chat or task turn)",
            )
        if scope == "chat":
            scope_chat_id = chat["id"]
        else:
            scope_project_id = chat.get("project_id") or ""
            if not scope_project_id:
                raise HTTPException(
                    status_code=400,
                    detail='scope="project" needs a chat that belongs to a '
                           "delegation project — this session's chat has none "
                           "(delegate with project_id first, or pin from a "
                           "project lane)",
                )

    scope_root = (
        agent_dir / "users" / username / "workspace"
        if username else agent_dir / "workspace"
    )
    target = scope_root / "apps" / f"{slug}.html"
    # Path derived from the validated slug only, but assert confinement
    # anyway — this write is otherwise un-RBAC-gated.
    if not target.resolve().is_relative_to(agent_dir.resolve()):
        raise HTTPException(status_code=400, detail="invalid slug")
    rel = target.relative_to(agent_dir).as_posix()

    # Satellite-side edits reach the platform tree only at turn boundaries.
    # An html-less pin — the slug-only refresh after a native edit of
    # apps/<slug>.html, or a re-pin over a hard-unpinned file — must serve
    # the satellite's CURRENT bytes, so read the file through first (the
    # display hooks' path: probe + pull under the per-path lock; a failed
    # pull leaves today's platform copy). A pin WITH html is authoritative
    # and never pulls (it pushes instead, below).
    if not req.html.strip():
        from core.remote import remote_file_flow
        if remote_file_flow.is_remote_session(req.session_id):
            pulled = await remote_file_flow.pull_through(req.session_id, rel)
            logger.info(
                "Hook app pin: html-less pin on a remote session — read %s "
                "through from the satellite (%s)",
                rel, "ok" if pulled is not None else "platform copy kept",
            )

    existing = await asyncio.to_thread(task_store.get_app_by_slug, ctx.agent, username, slug)
    if existing is not None:
        # Slugs share one namespace per (agent, caller scope): a slug held by
        # a pin of a DIFFERENT scope is refused instead of silently converted
        # (converting would yank a standing app off the strip, or move a
        # dashboard between chats).
        held = (existing.get("scope_chat_id") or "",
                existing.get("scope_project_id") or "")
        if held != (scope_chat_id, scope_project_id):
            kind = ("a chat-scoped" if held[0] else
                    "a project-scoped" if held[1] else "a standing")
            raise HTTPException(
                status_code=400,
                detail=f"slug '{slug}' already names {kind} pin in your "
                       "scope — pick another slug (scoped pins may reuse "
                       "their own slug to update)",
            )
    reused_file = False
    if existing is None:
        if not req.html.strip():
            # No row, no html — but the FIXED path may still hold the file
            # from a hard-unpinned registration: registering over it makes
            # re-pin a one-liner (unpin keeps the file by design).
            if not await asyncio.to_thread(target.is_file):
                raise HTTPException(
                    status_code=400,
                    detail=f"html is required on first pin (no apps/{slug}.html in your scope yet)",
                )
            reused_file = True
        if scope == "standing":
            # Scoped pins skip the cap: one-per-scope by construction.
            count = await asyncio.to_thread(task_store.count_apps, ctx.agent, username)
            if count >= task_store.MAX_APPS_PER_SCOPE:
                raise HTTPException(
                    status_code=400,
                    detail=f"app limit reached ({task_store.MAX_APPS_PER_SCOPE}) — unpin one first",
                )

    actions_json: str | None = None
    if req.actions is not None:
        actions_json, err = await asyncio.to_thread(
            _mf.validate_actions, req.actions, ctx.agent, not username,
        )
        if actions_json is None:
            raise HTTPException(status_code=400, detail=err)

    if req.html.strip():
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, req.html, "utf-8")
        # Same-turn satellite push (call-time import: uploads ↔ hooks cycle).
        from api.media.uploads import _push_upload_to_active_remote_sessions
        await _push_upload_to_active_remote_sessions(ctx.agent, rel, target)

    restored = bool(existing and existing.get("hidden"))
    replaced = ""
    if scope == "standing":
        row = await asyncio.to_thread(
            task_store.upsert_app,
            ctx.agent, username, owner_sub, slug,
            title=title or (slug if existing is None else None),
            rel_path=rel,
            actions_json=actions_json,
            make_default=req.make_default,
        )
    else:
        old = await asyncio.to_thread(
            task_store.get_scoped_app,
            chat_id=scope_chat_id, project_id=scope_project_id,
        )
        if old and (old["agent"], old["username"], old["slug"]) \
                != (ctx.agent, username, slug):
            replaced = old["slug"]
        row = await asyncio.to_thread(
            task_store.upsert_scoped_app,
            ctx.agent, username, owner_sub, slug,
            scope_chat_id=scope_chat_id,
            scope_project_id=scope_project_id,
            title=title or None,
            rel_path=rel,
            actions_json=actions_json,
        )
    # Broadcast on EVERY pin, not just html writes: a revived/registered row
    # must refresh open overlay tab strips (they invalidate the registry on
    # any apps/*.html file_updated), and re-wrapping an unchanged file is
    # harmless.
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(ctx.agent, rel, source="disk")
    approved = task_store.app_actions_approved(row)
    has_actions = bool(_mf.parse_actions(row))
    logger.info(
        f"Hook app pin: session={req.session_id}, agent={ctx.agent}, "
        f"slug={slug}, scope={'shared' if not username else username}, "
        f"pin_scope={scope}, approved={approved}, restored={restored}, "
        f"replaced={replaced or '-'}, reused_file={reused_file}"
    )
    out = {
        "status": "ok",
        "app_id": row["id"],
        "path": "/" + rel,
        "scope": "shared" if not username else "personal",
        "pin_scope": scope,
        "actions_approved": approved,
        "approval": ("approved" if approved else "pending user approval")
                    if has_actions else "none",
    }
    if replaced:
        out["replaced"] = (
            f"replaced the {scope}'s previous pin '{replaced}' — the scope "
            "holds exactly one dashboard"
            + (" (approval carried over — same manifest)" if approved and
               has_actions else "")
        )
    elif restored:
        out["restored"] = ("re-pinned the app the user had unpinned from the "
                           "dashboard — manifest and approval carried over")
    elif reused_file:
        out["reused_file"] = f"registered over the existing apps/{slug}.html"
    return out


@router.post("/v1/hooks/apps/unpin")
async def hook_app_unpin(req: HookAppSlugRequest, authorization: str | None = Header(None)):
    """Retire a pinned mini-app: the HARD delete (registration, manifest and
    approval all go; the workspace ``.html`` is the user's artifact and
    stays). The dashboard's X is the soft variant — it only hides the row,
    and this hook still finds hidden rows so "delete it entirely" works."""
    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    slug = req.slug.strip().lower()
    vis = (req.visibility or "").strip().lower()
    if vis and vis not in ("user", "agent"):
        raise HTTPException(status_code=400,
                            detail='visibility must be "user" or "agent"')

    def _find() -> dict[str, dict]:
        # The caller's reachable namespaces (S1): shared always (role-gated
        # below), personal when the session has a human. Auto-detect keeps
        # the pre-S1 one-arg call working for unambiguous slugs.
        found: dict[str, dict] = {}
        if vis in ("", "agent"):
            r = task_store.get_app_by_slug(ctx.agent, "", slug)
            if r is not None:
                found["agent"] = r
        if vis in ("", "user") and ctx.username:
            r = task_store.get_app_by_slug(ctx.agent, ctx.username, slug)
            if r is not None:
                found["user"] = r
        return found

    rows = await asyncio.to_thread(_find)
    if not rows:
        raise HTTPException(status_code=404, detail="no pinned app with that slug in your scope")
    if len(rows) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"slug '{slug}' is pinned both shared and personal — "
                   'pass visibility="user" or "agent" to pick one',
        )
    found_vis, row = next(iter(rows.items()))
    if found_vis == "agent":
        _require_shared_pin_authority(ctx, "")
    await asyncio.to_thread(task_store.delete_app, row["id"])
    logger.info(f"Hook app unpin: session={req.session_id}, slug={req.slug}, "
                f"visibility={found_vis}")
    return {"status": "ok", "kept_file": row["rel_path"]}


@router.post("/v1/hooks/apps/list")
async def hook_app_list(req: HookAppSlugRequest, authorization: str | None = Header(None)):
    """The caller-scope app list (shared + the session user's personal rows)
    so the agent reuses slugs deliberately instead of guessing. Includes
    soft-unpinned rows flagged ``unpinned`` — pin_app(slug) restores one
    with its manifest and approval intact. Chat/project-scoped Dock pins are
    appended after the standing list with their ``pin_scope``."""
    from api.apps import manifest as _mf

    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    username, _ = _app_scope(ctx)
    rows = await asyncio.to_thread(
        task_store.list_apps, ctx.agent, username, include_hidden=True,
    )
    rows += await asyncio.to_thread(
        task_store.list_scoped_apps, ctx.agent, username,
    )
    return {"apps": [{
        "slug": r["slug"],
        "title": r["title"],
        "scope": "shared" if not r["username"] else "personal",
        "pin_scope": ("chat" if r.get("scope_chat_id")
                      else "project" if r.get("scope_project_id")
                      else "standing"),
        "path": "/" + r["rel_path"],
        "position": r["position"],
        "actions": [{k: a.get(k) for k in ("id", "label", "type")}
                    for a in _mf.parse_actions(r)],
        "actions_approved": task_store.app_actions_approved(r),
        "updated_at": r["updated_at"],
        **({"unpinned": "user removed it from the dashboard — "
                        "pin_app(slug) restores it (approval intact)"}
           if r.get("hidden") else {}),
    } for r in rows]}


class HookFilePinRequest(BaseModel):
    session_id: str
    path: str = ""        # workspace-relative ("projects/x/plan.md"); an
    #                       agent-root path (workspace/…, users/…, knowledge/…)
    #                       is accepted too. Empty on unpin = the whole scope.
    title: str = ""       # empty → the filename
    # "chat" (default) | "project" — which Dock the file rides. Scope ids are
    # SESSION-DERIVED only (the session's chat / its project), never
    # caller-supplied — same rule as the app pin hook.
    scope: str = "chat"


async def _file_pin_scope(req_session_id: str, scope: str) -> tuple[str, str]:
    """Session-derived (scope_chat_id, scope_project_id) for the file-pin
    hooks — the exact resolution the app pin hook uses."""
    if scope not in ("chat", "project"):
        raise HTTPException(status_code=400,
                            detail='scope must be "chat" or "project"')
    chat = await asyncio.to_thread(task_store.get_chat_by_session, req_session_id)
    if not chat:
        raise HTTPException(
            status_code=400,
            detail=f'scope="{scope}" needs a chat-bound session — this '
                   "session has no chat (pin from a chat or task turn)",
        )
    if scope == "chat":
        return chat["id"], ""
    project_id = chat.get("project_id") or ""
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail='scope="project" needs a chat that belongs to a '
                   "delegation project — this session's chat has none "
                   "(delegate with project_id first, or pin from a "
                   "project lane)",
        )
    return "", project_id


def _file_pin_candidates(ctx, path: str) -> list[str]:
    """Agent-root-relative candidate rel_paths for a caller-supplied pin
    path, LEXICAL only (no existence check — unpin must resolve for files
    deleted since). First candidate: relative to the caller's scope
    workspace (shared ``workspace/`` or ``users/<u>/workspace/`` — the way
    agents write paths); second: the path taken as agent-root-relative when
    it names a readable top-level area. Confinement + OAuth-dir denial
    applied to every candidate; traversal is refused."""
    from api.agents.files import _check_oauth_protected

    agent_dir = config.get_agent_dir(ctx.agent)
    p = (path or "").strip().strip("/")
    if not p or "\x00" in p:
        raise HTTPException(status_code=400, detail="path is required")
    candidates: list[str] = []
    scope_root = paths._session_scope_root(ctx, agent_dir)
    for base in (scope_root, agent_dir):
        if base is agent_dir and p.split("/", 1)[0] not in (
                "workspace", "users", "knowledge"):
            continue
        resolved = (base / p).resolve()
        if not resolved.is_relative_to(agent_dir.resolve()):
            raise HTTPException(status_code=403,
                                detail="path traversal not allowed")
        rel = resolved.relative_to(agent_dir.resolve()).as_posix()
        _check_oauth_protected(rel)
        if rel not in candidates:
            candidates.append(rel)
    return candidates


@router.post("/v1/hooks/files/pin")
async def hook_file_pin(req: HookFilePinRequest,
                        authorization: str | None = Header(None)):
    """Called by display-mcp's pin_file: pin a workspace FILE to the chat/
    project Dock. The Dock renders it read-only with board-file semantics
    (collapsed row → expand → markdown), reading the content through the
    files API — per-viewer path policy is enforced there, this hook only
    validates the AGENT-side reference (confinement, OAuth-dir denial,
    existence, text extension). Re-pinning the same path updates the title.
    Remote sessions pin the platform MIRROR path — content refreshes when
    the satellite syncs (end of turn)."""
    from api.agents.files import TEXT_EXTENSIONS

    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    title = req.title.strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title exceeds 200 characters")
    scope = (req.scope or "chat").strip().lower()
    scope_chat_id, scope_project_id = await _file_pin_scope(req.session_id, scope)

    agent_dir = config.get_agent_dir(ctx.agent)
    rel = ""
    for cand in _file_pin_candidates(ctx, req.path):
        if await asyncio.to_thread((agent_dir / cand).is_file):
            rel = cand
            break
    if not rel:
        raise HTTPException(
            status_code=404,
            detail=f"no file at '{req.path}' in your workspace — pin an "
                   "existing file (the Dock renders it, it can't create it)",
        )
    ext = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if ext not in TEXT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"'{ext or rel}' is not a renderable text type — file pins "
                   "render markdown/text only "
                   f"({', '.join(sorted(TEXT_EXTENSIONS))})",
        )

    pins = await asyncio.to_thread(
        task_store.list_file_pins,
        chat_id=scope_chat_id, project_id=scope_project_id,
    )
    if (rel not in {p["rel_path"] for p in pins}
            and len(pins) >= task_store.MAX_FILE_PINS_PER_SCOPE):
        raise HTTPException(
            status_code=400,
            detail=f"file-pin limit reached "
                   f"({task_store.MAX_FILE_PINS_PER_SCOPE} per {scope}) — "
                   "unpin one first",
        )
    row = await asyncio.to_thread(
        task_store.upsert_file_pin,
        ctx.agent, rel,
        scope_chat_id=scope_chat_id, scope_project_id=scope_project_id,
        title=title or rel.rsplit("/", 1)[-1],
    )
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(
        ctx.agent, rel, source="disk", pin=True,
    )
    logger.info(
        f"Hook file pin: session={req.session_id}, agent={ctx.agent}, "
        f"rel={rel}, pin_scope={scope}"
    )
    return {
        "status": "ok",
        "pin_id": row["id"],
        "path": "/" + rel,
        "pin_scope": scope,
        "title": row["title"],
        "note": "read-only Dock row — collapsed by default, renders live "
                "from the file (edits show within seconds; remote files on "
                "the next sync)",
    }


@router.post("/v1/hooks/files/unpin")
async def hook_file_unpin(req: HookFilePinRequest,
                          authorization: str | None = Header(None)):
    """Remove a Dock file pin (the file itself is never touched). With
    ``path`` empty, clears every pin of the scope."""
    verify_session_match(authorization, req.session_id)
    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    scope = (req.scope or "chat").strip().lower()
    scope_chat_id, scope_project_id = await _file_pin_scope(req.session_id, scope)

    removed = 0
    rels: list[str] = []
    if req.path.strip():
        # Lexical resolution — the pinned file may be gone from disk.
        rels = _file_pin_candidates(ctx, req.path)
        for rel in rels:
            removed += await asyncio.to_thread(
                task_store.delete_file_pins,
                chat_id=scope_chat_id, project_id=scope_project_id,
                rel_path=rel,
            )
    else:
        pins = await asyncio.to_thread(
            task_store.list_file_pins,
            chat_id=scope_chat_id, project_id=scope_project_id,
        )
        rels = [p["rel_path"] for p in pins]
        removed = await asyncio.to_thread(
            task_store.delete_file_pins,
            chat_id=scope_chat_id, project_id=scope_project_id,
        )
    if not removed:
        raise HTTPException(status_code=404,
                            detail="no matching file pin in that scope")
    from services.notifications import notification_manager
    for rel in rels:
        await notification_manager.broadcast_file_updated(
            ctx.agent, rel, source="disk", pin=True,
        )
    logger.info(
        f"Hook file unpin: session={req.session_id}, agent={ctx.agent}, "
        f"removed={removed}, pin_scope={scope}"
    )
    return {"status": "ok", "removed": removed, "kept_files": True}


# Cache WOPI URLs per file_id+user so Collabora sees one session, not many.
# Key: (file_id, user_sub) → {"wopi_url": str, "token_ttl": int, "expires": float}
