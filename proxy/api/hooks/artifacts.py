"""Display artifacts pushed from inside a session: images, image generation
progress, urls, files, media and app UI.

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from storage import database as task_store
from storage.pg import run_db
from auth.path_policy import (
    check_host_path_access,
)
from api.sessions.sessions import verify_session_match_async
from core.session.session_state import (
    get_permission_queue,
    get_session_security,
)
from api.hooks import paths, routing
from core.session import session_kind
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")
router = APIRouter()


class HookImagesItem(BaseModel):
    """One image inside a `/v1/hooks/images` payload.

    Exactly one of ``url`` (external CDN — browser fetches directly) or
    ``image_data`` (base64, for local files the MCP already read) must be set.
    """
    url: str = ""
    image_data: str = ""
    mime_type: str = "image/jpeg"
    caption: str = ""
    attribution: str = ""
    link_url: str = ""
    download_url: str = ""


class HookImagesRequest(BaseModel):
    session_id: str
    images: list[HookImagesItem]


@router.post("/v1/hooks/images")
async def hook_images(req: HookImagesRequest, authorization: str | None = Header(None)):
    """Called by display-mcp / image-gen-mcp / file-tools-mcp / image-search-mcp
    to push an inline image gallery (1-N images) to the chat.

    The dashboard renders 1 image as a single card, 2-3 as a row, 4+ as a
    horizontal scroll-snap carousel — that decision lives in the renderer,
    not here.
    """
    await verify_session_match_async(authorization, req.session_id)
    if not req.images:
        raise HTTPException(status_code=400, detail="images list cannot be empty")
    for idx, item in enumerate(req.images):
        has_url = bool(item.url)
        has_data = bool(item.image_data)
        if has_url == has_data:
            # Either both set or neither — both are invalid.
            raise HTTPException(
                status_code=400,
                detail=f"images[{idx}] must have exactly one of url, image_data",
            )
    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": wire.IMAGES,
        "images": [item.model_dump() for item in req.images],
    })
    logger.info(
        f"Hook images: session={req.session_id}, count={len(req.images)}, "
        f"first_caption={req.images[0].caption[:50] if req.images else ''}"
    )
    return {"status": "ok"}


class HookImageGeneratingRequest(BaseModel):
    session_id: str
    prompt_preview: str = ""
    model: str = "nano-banana"


@router.post("/v1/hooks/image-generating")
async def hook_image_generating(req: HookImageGeneratingRequest, authorization: str | None = Header(None)):
    """Called by image-gen-mcp before starting generation. Shows skeleton placeholder."""
    await verify_session_match_async(authorization, req.session_id)
    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": wire.IMAGE_GENERATING,
        "prompt_preview": req.prompt_preview,
        "model": req.model,
    })
    return {"status": "ok"}


class HookImageGenFailedRequest(BaseModel):
    session_id: str


@router.post("/v1/hooks/image-gen-failed")
async def hook_image_gen_failed(req: HookImageGenFailedRequest, authorization: str | None = Header(None)):
    """Called by image-gen-mcp when generation fails. Removes skeleton placeholder."""
    await verify_session_match_async(authorization, req.session_id)
    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({"event_type": wire.IMAGE_GEN_FAILED})
    return {"status": "ok"}


class HookUrlRequest(BaseModel):
    session_id: str
    url: str
    title: str
    description: str = ""


@router.post("/v1/hooks/url")
async def hook_url(req: HookUrlRequest, authorization: str | None = Header(None)):
    """Called by display-mcp to push a clickable link to the chat."""
    await verify_session_match_async(authorization, req.session_id)
    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": wire.URL,
        "url": req.url,
        "title": req.title,
        "description": req.description,
    })
    logger.info(f"Hook url: session={req.session_id}, url={req.url}")
    return {"status": "ok"}


class HookFileRequest(BaseModel):
    session_id: str
    path: str
    filename: str = ""
    description: str = ""


@router.post("/v1/hooks/file")
async def hook_file(req: HookFileRequest, authorization: str | None = Header(None)):
    """Called by display-mcp to push a downloadable file to the chat.

    Routes through the session's adapter to handle delivery (e.g. dashboard
    serves from proxy via token URLs). For remote sessions, the file is
    lazily pulled from the satellite into the platform-side cache first so
    the adapter can serve it directly.
    """
    await verify_session_match_async(authorization, req.session_id)

    file_path, resolution = await paths._classify_and_pull(req.session_id, req.path)
    if file_path is None:
        detail = (
            resolution.error
            if resolution is not None and not resolution.allowed
            else f"File not found: {req.path}"
        )
        raise HTTPException(status_code=400, detail=detail)

    filename = req.filename or file_path.name

    # Route through adapter based on session's client_type. Meeting
    # participants carry client_type "meeting" (no adapter of its own) but
    # render into the parent DASHBOARD chat — serve them there.
    from adapters import get_adapter, get_session_adapter
    route = routing.resolve_hook_route(req.session_id)
    if route.is_meeting:
        adapter = get_adapter(session_kind.DASHBOARD.name)
    else:
        adapter = get_session_adapter(req.session_id)
    if adapter is None:
        raise HTTPException(
            status_code=400,
            detail=f"No adapter found for session {req.session_id} -- client_type not set",
        )

    # The hook owns download-token minting (it holds the chat + security
    # context the adapter doesn't); the adapter only shapes the event. Durable
    # media_tokens row → the download button in chat HISTORY keeps working
    # across restarts (the old in-memory token died after 1h). Served by
    # /v1/media (cookie-gated; media_kind "file" is attachment-forced).
    download_url = ""
    if getattr(adapter, "serves_file_downloads", False):
        chat_id = await routing.resolve_hook_chat_id(req.session_id) or None
        sec = get_session_security(req.session_id)
        download_token = secrets.token_urlsafe(32)
        await run_db(
            task_store.create_media_token,
            download_token,
            str(file_path),
            media_kind="file",
            chat_id=chat_id,
            session_id=req.session_id,
            # cache_owned False even for satellite-pulled cache copies: several
            # tokens may share one cached file; the cache has its own lifecycle.
            cache_owned=False,
            expires_at="",  # durable until the chat is deleted
            agent=sec.agent if sec else "",
        )
        # No fn= here: the dashboard appends it from the file's name
        # (baking it in too would duplicate the param).
        download_url = f"/v1/media/{download_token}?download=1"

    result = await adapter.handle_file_display(
        req.session_id, file_path, filename, req.description, download_url,
    )

    queue = get_permission_queue(route.queue_session_id)
    await queue.put(result)
    logger.info(f"Hook file: session={req.session_id}, file={filename}")
    return {"status": "ok", **result}


class HookMediaRequest(BaseModel):
    session_id: str
    source: str
    media_kind: str  # "video" | "audio"
    caption: str = ""
    title: str = ""
    poster: str = ""  # video only; URL passthrough (local poster paths ignored)


@router.post("/v1/hooks/media")
async def hook_media(req: HookMediaRequest, authorization: str | None = Header(None)):
    """Called by display-mcp's display_video / display_audio to render a media
    player in the chat.

    Unlike images (base64-inlined into the event), media is served over HTTP
    with Range support via a capability token, so the file is never embedded in
    the event/DB. Web URLs pass straight through to ``<video src>``. Local /
    agent-tree / satellite-host paths are resolved to a proxy-local file (the
    satellite-host case is lazily pulled ≤50MB), made browser-playable, and
    minted a durable ``media_tokens`` row.
    """
    await verify_session_match_async(authorization, req.session_id)

    kind = req.media_kind.strip().lower()
    if kind not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="media_kind must be 'video' or 'audio'")
    source = (req.source or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source is required")

    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)

    # Web URL → browser fetches the origin directly (Range handled there).
    if source.startswith(("http://", "https://")):
        await queue.put({
            "event_type": kind,
            "src_kind": "url",
            "url": source,
            "mime": "",
            "caption": req.caption,
            "title": req.title,
            "poster": req.poster if req.poster.startswith(("http://", "https://")) else "",
        })
        logger.info(f"Hook media({kind}): session={req.session_id}, url")
        return {"status": "ok"}

    # Resolve to a proxy-local file via the shared classify-first resolver:
    # synced-tree paths pull_through into the workspace, satellite-host paths
    # (Desktop/Downloads) lazy-pull into the proxy cache (≤100MB), and the
    # credential/.ssh/.env denylist applies for remote sessions.
    from services.media import media_pipeline
    from core.remote import remote_file_flow

    local_path, resolution = await paths._classify_and_pull(req.session_id, source)
    if local_path is None:
        detail = (
            resolution.error
            if resolution is not None and not resolution.allowed
            else f"media not reachable: {source} "
                 "(remote non-workspace files are limited to 100MB)"
        )
        raise HTTPException(status_code=400, detail=detail)
    # Satellite-host pulls land in the throwaway host cache (purged on chat
    # delete); synced/local files live in the workspace and are not cache-owned.
    cache_owned = remote_file_flow.is_host_cache_path(str(local_path))

    # Probe codecs; if we'll transcode, show a skeleton first so the chat isn't
    # blank while ffmpeg runs (the real block replaces it on completion).
    codecs = await media_pipeline.probe(local_path)
    if media_pipeline.needs_transcode(local_path, codecs) and media_pipeline.ffmpeg_available():
        await queue.put({
            "event_type": wire.MEDIA_PROCESSING,
            "media_kind": kind,
            "caption": req.caption,
        })
    # Satellite-host (Desktop/Downloads) media is re-pullable, not retained:
    # route its derivatives to the TTL'd host cache and record the origin path
    # so serve_media can re-pull from the laptop on replay.
    origin_path = ""
    media_dest = None
    if (
        resolution is not None
        and resolution.path_ref is not None
        and resolution.path_ref.kind == "satellite_host"
    ):
        origin_path = resolution.path_ref.value
        media_dest = media_pipeline.host_cache_dir()
    served_path, mime, transcode_cache = await media_pipeline.ensure_playable_async(
        local_path, codecs=codecs, media_kind=kind, dest_dir=media_dest,
    )
    cache_owned = cache_owned or transcode_cache
    resolved_kind = media_pipeline.media_kind_from_mime(mime) or kind

    chat_id = await routing.resolve_hook_chat_id(req.session_id) or None
    info = remote_file_flow._get_remote_session_info(req.session_id)
    machine_id = getattr(info, "machine_id", None)
    sec = get_session_security(req.session_id)

    token = secrets.token_urlsafe(32)
    await run_db(
        task_store.create_media_token,
        token,
        str(served_path),
        mime=mime,
        media_kind=resolved_kind,
        chat_id=chat_id,
        session_id=req.session_id,
        machine_id=machine_id,
        cache_owned=cache_owned,
        expires_at="",  # durable until the chat is deleted
        origin_path=origin_path,
        agent=sec.agent if sec else "",
    )

    await queue.put({
        "event_type": resolved_kind,
        "src_kind": "token",
        "token": token,
        "media_url": f"/v1/media/{token}",
        "mime": mime,
        "caption": req.caption,
        "title": req.title,
        "poster": req.poster if req.poster.startswith(("http://", "https://")) else "",
    })
    logger.info(
        f"Hook media({resolved_kind}): session={req.session_id}, "
        f"token={token[:8]}..., cache_owned={cache_owned}"
    )
    return {"status": "ok"}


class HookUiRequest(BaseModel):
    session_id: str
    html: str
    title: str = ""
    height: int | None = None  # fixed pixel height hint; None = auto-size
    save_path: str = ""  # sandbox-virtual; MCP sends its scope-correct default
    display: bool = True  # False = save the file only, no chat block/token


# Explicit cap under the global MAX_REQUEST_BODY_BYTES backstop: an artifact
# is a chat block, not a file transfer.
_UI_MAX_HTML_BYTES = 2 * 1024 * 1024


def _ui_slug(title: str) -> str:
    """Filename slug from an artifact title ('Tip calculator' → 'tip-calculator')."""
    s = "".join(c if c.isalnum() else "-" for c in title.lower())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:40] or "ui"


@router.post("/v1/hooks/ui")
async def hook_ui(req: HookUiRequest, authorization: str | None = Header(None)):
    """Called by display-mcp's display_ui to render an HTML artifact in the chat.

    The raw agent content is written VERBATIM to the caller's workspace
    (wrapping/theming happens at serve time in ``/v1/ui/{token}`` — the agent
    can Read/Edit the file it just created, and historical artifacts pick up
    wrapper improvements automatically), pushed to active remote sessions, and
    minted a durable ``media_tokens`` row (``media_kind="ui"``) that only the
    sandboxed ``/v1/ui`` route will serve.
    """
    await verify_session_match_async(authorization, req.session_id)

    if not req.html.strip():
        raise HTTPException(status_code=400, detail="html is required")
    if len(req.html.encode("utf-8", errors="ignore")) > _UI_MAX_HTML_BYTES:
        raise HTTPException(status_code=400, detail="html exceeds the 2MB artifact cap")
    title = req.title.strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="title exceeds 200 characters")

    ctx = get_session_security(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="unknown session")
    agent_dir = config.get_agent_dir(ctx.agent)

    # The caller's own scope workspace: the default save dir, the anchor for
    # relative paths, and the re-anchor target for denied/escaping explicit
    # paths. The hook owns the default (NOT the MCP): on satellites the MCP's
    # OTO_WORKSPACE_DIR is rewritten to a satellite-absolute path the proxy
    # could not resolve.
    # MOUNT identity, not attribution: a Shared-only human chat keeps
    # ctx.username for attribution but works in the AGENT scope — its
    # artifacts belong in the shared workspace (same rule as the MCP
    # framework's OTO_WORKSPACE_DIR injection). An external caller's
    # artifacts belong in THEIR tree.
    scope_root = paths._session_scope_root(ctx, agent_dir)
    raw = req.save_path.strip()
    if not raw:
        raw = f"generated-ui/{_ui_slug(title)}-{secrets.token_hex(4)}.html"
    if raw.startswith("/"):
        # Sandbox-virtual — resolved in the CALLER'S scope (a viewer's
        # /workspace/x.html lands in THEIR users/{u}/workspace).
        target = Path(paths._sandbox_to_host(raw, ctx, agent_dir))
    else:
        # Documented workspace-relative form.
        target = scope_root / raw
    if target.suffix.lower() != ".html":
        target = target.with_suffix(".html")
    # Re-gate the resolved host path — paths._sandbox_to_host is purely lexical and
    # the write below is otherwise un-RBAC-gated. Anything escaping the tree
    # or denied by the role matrix re-anchors to the caller's generated-ui/.
    allowed = False
    try:
        if target.resolve().is_relative_to(agent_dir.resolve()):
            allowed = check_host_path_access(target, ctx, writing=True).allowed
    except (OSError, ValueError):
        allowed = False
    if not allowed:
        target = scope_root / "generated-ui" / Path(raw).name
        if target.suffix.lower() != ".html":
            target = target.with_suffix(".html")

    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, req.html, "utf-8")
    rel = target.relative_to(agent_dir).as_posix()
    sandbox_path = "/" + rel

    # Same-turn push so a satellite CLI can immediately Read the file it just
    # created (call-time import: uploads ↔ hooks would otherwise cycle).
    from api.media.uploads import _push_upload_to_active_remote_sessions
    await _push_upload_to_active_remote_sessions(ctx.agent, rel, target)

    # Live update-in-place (mirrors pin_app): /v1/ui serves the FILE at
    # request time, so overwriting a prior artifact's save_path already
    # changes what every minted token serves — this broadcast makes open
    # renders (inline blocks, PiP windows) reload NOW. With display=False
    # that's the whole delivery: a standing artifact updated silently across
    # turns, no new chat block.
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(ctx.agent, rel, source="disk")

    if not req.display:
        logger.info(f"Hook ui (save-only): session={req.session_id}, path={rel}")
        return {"status": "ok", "path": sandbox_path}

    chat_id = await routing.resolve_hook_chat_id(req.session_id) or None
    token = secrets.token_urlsafe(32)
    # cache_owned MUST stay False: True would make the chat-delete reap unlink
    # the user's workspace .html — the artifact file outlives its chat.
    await run_db(
        task_store.create_media_token,
        token,
        str(target),
        mime="text/html",
        media_kind="ui",
        chat_id=chat_id,
        session_id=req.session_id,
        machine_id=None,
        cache_owned=False,
        expires_at="",  # durable until the chat is deleted
        agent=ctx.agent,
    )

    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": wire.UI,
        "token": token,
        "ui_url": f"/v1/ui/{token}",
        "title": title,
        "height": req.height,
        "path": rel,
    })
    logger.info(f"Hook ui: session={req.session_id}, token={token[:8]}..., path={rel}")
    return {"status": "ok", "path": sandbox_path, "ui_url": f"/v1/ui/{token}"}
