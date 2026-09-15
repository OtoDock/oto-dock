"""The document preview hook (WOPI url cache included).

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import logging
import secrets
import time
import urllib.parse

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from storage import database as task_store
from api.sessions.sessions import verify_session_match
from core.session.session_state import (
    _sessions,
    get_permission_queue,
    get_session_security,
)
from api.hooks import paths, routing

logger = logging.getLogger("claude-proxy")
router = APIRouter()


_wopi_url_cache: dict[tuple[str, str], dict] = {}


class HookDocumentPreviewRequest(BaseModel):
    session_id: str
    file_path: str
    filename: str = ""


@router.post("/v1/hooks/document-preview")
async def hook_document_preview(req: HookDocumentPreviewRequest,
                                 authorization: str | None = Header(None)):
    """Called by file-tools MCP to push a live Collabora preview to the chat."""
    verify_session_match(authorization, req.session_id)

    import config as cfg
    from api.media.wopi import encode_file_id, create_wopi_token

    if not cfg.COLLABORA_URL:
        # Match the loud-fail behaviour of /v1/documents/wopi-url. Without this,
        # an empty COLLABORA_URL silently produces a relative iframe src and the
        # SPA catch-all serves the dashboard's index.html — user sees the home
        # page inside the preview iframe with no clue why.
        raise HTTPException(
            status_code=503,
            detail="COLLABORA_URL is not configured — set it in config.env and restart the proxy.",
        )

    file_path, resolution = await paths._classify_and_pull(req.session_id, req.file_path)
    if file_path is None:
        detail = (
            resolution.error
            if resolution is not None and not resolution.allowed
            else f"File not found: {req.file_path}"
        )
        raise HTTPException(status_code=400, detail=detail)

    filename = req.filename or file_path.name

    # Compute relative path from AGENTS_DIR
    try:
        rel_path = str(file_path.resolve().relative_to(cfg.AGENTS_DIR.resolve()))
    except ValueError:
        raise HTTPException(status_code=400, detail="File must be within agents directory")

    file_id = encode_file_id(rel_path)

    # Role-gate WRITE capability on the inline Collabora token. The
    # session's authenticated SecurityContext carries the previewing human's
    # effective per-agent role + username — the same values the satellite
    # write-back guard uses. A viewer iterating on a SHARED workspace file gets a
    # view-only inline preview (can't save); an editor/manager — or ANY role on
    # their OWN users/{u}/ files — gets edit. Fail-closed to "view" when there's
    # no security context.
    #
    # The token still mints user_sub="agent" / user_name="Agent" so the existing
    # two-participant UX (the agent + the human's own workspace-tab session) is
    # unchanged — ONLY write capability is gated.
    session_meta = _sessions.get(req.session_id, {})
    user_sub = "agent"
    user_name = "Agent"
    from core.remote.file_sync import can_write_back
    sec = get_session_security(req.session_id)
    tree_rel = rel_path.partition("/")[2]  # strip "<agent>/" → agent-tree-relative
    permissions = "view"
    # Host-cache doc (a Desktop/Downloads file pulled through THIS session's
    # cache): the agent-tree write matrix doesn't apply — the platform copy is
    # a mirror of a file on the session's own machine. Grant edit to
    # write-capable roles; PutFile pushes the bytes back to the real file
    # (wopi.wopi_put_file), where the satellite's own host write policy is the
    # second, authoritative gate. Session-scoped: only THIS session's cache
    # root qualifies, so another session's cached files stay view-only here.
    _is_host_cache = False
    if sec is not None:
        from core.remote import remote_file_flow
        try:
            _is_host_cache = file_path.resolve().is_relative_to(
                remote_file_flow.host_cache_session_root(req.session_id).resolve()
            )
        except OSError:
            _is_host_cache = False
    if _is_host_cache:
        if (getattr(sec, "role", "") or "") in ("editor", "manager", "admin"):
            permissions = "edit"
    elif sec is not None:
        from core.remote.file_sync import library_mirror_source
        _wl = None
        if library_mirror_source(tree_rel) is not None:
            from storage.knowledge import db_knowledge_libraries
            _wl = db_knowledge_libraries.writable_pairs_for(
                getattr(sec, "agent", "") or "")
        if can_write_back(
            tree_rel, getattr(sec, "role", "") or "",
            getattr(sec, "username", "") or "",
            mount_username=getattr(sec, "mount_username", None),
            writable_libraries=_wl,
        ):
            permissions = "edit"

    # Reuse an existing WOPI URL if still valid (avoids spawning multiple
    # Collabora sessions). Keyed by permission too, so a viewer never receives a
    # cached edit token — or vice-versa — for the same shared file_id.
    cache_key = (file_id, permissions)
    cached = _wopi_url_cache.get(cache_key)
    if cached and cached["expires"] > time.time():
        wopi_url = cached["wopi_url"]
    else:
        wopi_token, token_ttl = create_wopi_token(
            rel_path, user_sub, user_name, permissions, session_meta.get("agent", "")
        )

        wopi_src = urllib.parse.quote(
            f"{cfg.WOPI_BASE_URL.rstrip('/')}/wopi/files/{file_id}",
            safe="",
        )
        wopi_url = (
            f"{cfg.COLLABORA_URL}/browser/dist/cool.html"
            f"?WOPISrc={wopi_src}"
            f"&access_token={wopi_token}"
            f"&access_token_ttl={token_ttl}"
            f"&closebutton=0&homebutton=0"
            f"&ui_defaults=UIMode%3Dcompact%3BTextSidebar%3Dfalse"
            f"%3BSpreadsheetSidebar%3Dfalse%3BPresentationSidebar%3Dfalse"
        )
        _wopi_url_cache[cache_key] = {
            "wopi_url": wopi_url,
            "expires": time.time() + 12600,  # cache for 3.5 hours (token lasts 4 hours)
        }

    # Append a timestamp so the iframe reloads on file changes (same token, new URL key)
    wopi_url_with_ts = f"{wopi_url}&_t={int(time.time())}"

    chat_id = await routing.resolve_hook_chat_id(req.session_id) or None

    # Version-pinned snapshot: copy the file AS DELIVERED — right here, at
    # push time, before the agent can touch it again — into the proxy-private
    # snapshot cache. When a later push supersedes this preview, the dashboard
    # swaps the old block to a view-only render of this copy ("previous
    # version"). Best-effort: with no snapshot the superseded block degrades
    # to the "preview moved" chip.
    snapshot_id = ""
    generation = int(time.time() * 1000)
    if chat_id:
        from services.media import preview_snapshots
        snapshot_id = await asyncio.to_thread(
            preview_snapshots.create_snapshot, chat_id, file_path,
        ) or ""

    # Download token: durable media_tokens row (media_kind "file" →
    # attachment-forced by /v1/media's inline allowlist), so the preview's
    # download button survives restarts like the rest of the chat history.
    download_token = secrets.token_urlsafe(32)
    task_store.create_media_token(
        download_token,
        str(file_path),
        media_kind="file",
        chat_id=chat_id,
        session_id=req.session_id,
        cache_owned=False,
        expires_at="",  # durable until the chat is deleted
        agent=getattr(sec, "agent", "") or "",
    )
    # No fn= here: DocumentPreview appends it client-side from its filename
    # prop (baking it in too would duplicate the param).
    download_url = f"/v1/media/{download_token}?download=1"

    # Push to permission queue → stream pump → WS
    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": "document_preview",
        "wopi_url": wopi_url_with_ts,
        "filename": filename,
        "file_id": file_id,
        "download_url": download_url,
        "snapshot_id": snapshot_id,
        "generation": generation,
    })

    logger.info(f"Hook document-preview: session={req.session_id}, file={filename}")
    return {"status": "ok", "wopi_url": wopi_url, "file_id": file_id}
