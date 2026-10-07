"""The document preview hook (WOPI url cache included).

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import hashlib
import logging
import secrets
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from storage import database as task_store
from storage.pg import run_db
from api.sessions.sessions import verify_session_match_async
from core.session.session_state import (
    _sessions,
    get_permission_queue,
    get_session_security,
)
from api.hooks import paths, routing
from auth import roles
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")
router = APIRouter()


_wopi_token_cache: dict[tuple[str, ...], dict] = {}

# The document generation of each file's newest push, from whichever chat
# pushed it, with the sha256 of the bytes it pushed ("" when unknown: a seed
# from a stored row, a file past the hash cap). The WOPI id of every live
# load of the file is keyed on it (api/media/wopi.py: the pane's mint, the
# workspace tab's), so the loads after one push share a Collabora document
# and the first load after the next push opens a new one. In memory,
# bounded: after a restart the pane's mint seeds it with the chat's newest
# row. Touched on the event loop only.
_pushed_generations: OrderedDict[str, tuple[int, str]] = OrderedDict()
_PUSHED_GENERATIONS_MAX = 4096
_HASH_MAX_BYTES = 100 * 1024 * 1024


def note_pushed_generation(file_id: str, generation: int, digest: str = "") -> None:
    """Note the document generation of a push of the file (a newer one than
    the noted wins) with the sha256 of the bytes it pushed."""
    noted = _pushed_generations.get(file_id)
    if noted is None or generation >= noted[0]:
        _pushed_generations[file_id] = (generation, digest)
    _pushed_generations.move_to_end(file_id)
    while len(_pushed_generations) > _PUSHED_GENERATIONS_MAX:
        _pushed_generations.popitem(last=False)


def pushed_generation(file_id: str) -> int:
    """The document generation noted for a file, 0 when none."""
    noted = _pushed_generations.get(file_id)
    return noted[0] if noted is not None else 0


def mint_generation(file_id: str, row_generation: int) -> int:
    """The document generation a live mint opens: the noted one, else the
    chat's newest row's, which then seeds the map (a newer row replaces an
    older seed: after a restart chats mint in any order)."""
    noted = _pushed_generations.get(file_id)
    if noted is not None and (noted[1] or row_generation <= noted[0]):
        return noted[0]
    if row_generation:
        note_pushed_generation(file_id, row_generation)
    return row_generation


def file_digest(path: Path) -> str:
    """sha256 of a file, "" past the hash cap or unreadable. Blocking: a
    worker-thread call."""
    try:
        if path.stat().st_size > _HASH_MAX_BYTES:
            return ""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def unchanged_since_push(file_id: str, rel_path: str, digest: str) -> bool:
    """The file holds the bytes its newest push's document holds: its base
    (the bytes Collabora fetched or last saved, a person's save since the
    push included) when one is known, else the bytes pushed. A
    ``preview_document`` of an untouched file is no change: no new
    document, no ``file_updated``. The base comes first: a write back to
    the pushed bytes over a person's save is a change to their document."""
    noted = _pushed_generations.get(file_id)
    if not digest or noted is None:
        return False
    from api.media.wopi import _doc_base, encode_wopi_id
    base = _doc_base(encode_wopi_id(rel_path, noted[0]))
    return base == digest if base is not None else noted[1] == digest


def _person_claims(sub: str, tree_rel: str, host_cache: bool, agent: str) -> tuple[str, str]:
    """``(user_name, permissions)`` of a pushed token minted for a person:
    the name a dashboard mint gives them and the live mint's write decision
    for their own role on the agent. Store reads: call it on the DB
    executor."""
    from api.media.wopi import live_permissions
    from auth.providers import acting_role_of
    user = task_store.get_user(sub) or {}
    username = task_store.get_username_by_sub(sub) or ""
    name = user.get("name") or user.get("display_name") or username or "User"
    return name, live_permissions(tree_rel, acting_role_of(sub, agent), username, agent,
                                  host_cache=host_cache)


class HookDocumentPreviewRequest(BaseModel):
    session_id: str
    file_path: str
    filename: str = ""


@router.post("/v1/hooks/document-preview")
async def hook_document_preview(req: HookDocumentPreviewRequest,
                                 authorization: str | None = Header(None)):
    """Called by file-tools MCP to push a live Collabora preview to the chat."""
    await verify_session_match_async(authorization, req.session_id)

    import config as cfg
    from api.media.wopi import build_cool_url, encode_file_id, encode_wopi_id, create_wopi_token

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

    # The push's document generation, noted at once (a mint the write's own
    # file_updated triggers must already open it): the noted one when the
    # file still holds the bytes its document knows (a ``preview_document``
    # of an untouched file, on a remote session too, where the resolve has
    # just rewritten the same bytes), else a new one, strictly after it.
    now = time.time()
    generation = int(now * 1000)
    digest = await asyncio.to_thread(file_digest, file_path)
    noted = pushed_generation(file_id)
    if noted and unchanged_since_push(file_id, rel_path, digest):
        document_generation = noted
    else:
        document_generation = max(generation, noted + 1) if noted else generation
    note_pushed_generation(file_id, document_generation, digest)

    # Role-gate WRITE capability on the pushed Collabora token. The
    # session's authenticated SecurityContext carries the previewing human's
    # effective per-agent role + username — the same values the satellite
    # write-back guard uses. A viewer iterating on a SHARED workspace file gets a
    # view-only document (can't save); an editor/manager — or ANY role on
    # their OWN users/{u}/ files — gets edit. Fail-closed to "view" when there's
    # no security context. A turn that runs as a person (below) mints the
    # token for that person instead: their name and their own write decision.
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
        if roles.can_write_workspace(getattr(sec, "role", "") or ""):
            permissions = "edit"
    elif sec is not None:
        from core.remote.file_sync import library_mirror_source
        _wl = None
        if library_mirror_source(tree_rel) is not None:
            from storage.knowledge import db_knowledge_libraries
            _wl = await run_db(
                db_knowledge_libraries.writable_pairs_for,
                getattr(sec, "agent", "") or "")
        if can_write_back(
            tree_rel, getattr(sec, "role", "") or "",
            getattr(sec, "username", "") or "",
            mount_username=getattr(sec, "mount_username", None),
            writable_libraries=_wl,
        ):
            permissions = "edit"

    # The chat the push lands in: its pane's saves refresh this push's
    # version, so the token names it.
    chat_id = await routing.resolve_hook_chat_id(req.session_id) or None

    # The token reaches only the connections of the person the chat's turn
    # runs as (artifact_events.for_viewer: the pump's ``wake_person``, never
    # the chat row's owner, which is the agent on a shared chat), so it is
    # minted for them: a save from their pane reads "by <name>", records
    # them as the file's author and skips their own dashboards. No person
    # (a task run, a wake, a meeting) keeps the agent's token.
    person = ""
    queue_session_id = routing.resolve_hook_route(req.session_id).queue_session_id
    if chat_id:
        from core.events.stream_pump import _active_pumps
        pump = _active_pumps.get(chat_id)
        if pump is not None and not pump.is_done and pump.session_id == queue_session_id:
            person = pump.wake_person or ""
    if person:
        user_name, permissions = await run_db(
            _person_claims, person, tree_rel, _is_host_cache, getattr(sec, "agent", "") or "",
        )
        user_sub = person

    # Reuse a WOPI token while it is still valid (the WOPISrc, not the
    # token, decides which Collabora document a load joins). Keyed by
    # permission too, so a viewer never receives a
    # cached edit token — or vice-versa — for the same shared file_id, and by
    # chat, so a token never carries another chat's claim.
    cache_key = (file_id, permissions, chat_id or "", user_sub)
    cached = _wopi_token_cache.get(cache_key)
    if cached and cached["expires"] > now:
        wopi_token, token_ttl = cached["token"], cached["ttl"]
    else:
        wopi_token, token_ttl = create_wopi_token(
            rel_path, user_sub, user_name, permissions, session_meta.get("agent", ""),
            chat_id=chat_id or "",
        )
        for key in [k for k, v in _wopi_token_cache.items() if v["expires"] <= now]:
            del _wopi_token_cache[key]
        _wopi_token_cache[cache_key] = {
            "token": wopi_token, "ttl": token_ttl,
            "expires": now + 12600,  # cache for 3.5 hours (token lasts 4 hours)
        }

    # The host page URL carries no token. Its WOPISrc is keyed by the push's
    # document generation: Collabora opens a fresh document for it (the
    # stored bytes, whatever views of an earlier push linger). The event's
    # generation is the push time, which the pane orders and reloads on.
    wopi_url = build_cool_url(encode_wopi_id(rel_path, document_generation))

    # The push's version: copy the file AS DELIVERED — right here, at push
    # time, before the agent can touch it again — into the proxy-private
    # snapshot cache (the pane's Versions menu opens it read-only), number
    # the push among the file's pushes in the chat and trim the file's
    # oldest versions past the cap. Best-effort: with no copy the version
    # reads "no longer available".
    snapshot_id = ""
    version = 0
    if chat_id:
        from services.media import preview_snapshots
        snapshot_id = await asyncio.to_thread(
            preview_snapshots.create_snapshot, chat_id, file_path,
        ) or ""
        version = await run_db(preview_snapshots.stamp_and_cap, chat_id, file_id)

    # Download token: durable media_tokens row (media_kind "file" →
    # attachment-forced by /v1/media's inline allowlist), so the preview's
    # download button survives restarts like the rest of the chat history.
    download_token = secrets.token_urlsafe(32)
    await run_db(
        task_store.create_media_token,
        download_token,
        str(file_path),
        media_kind="file",
        chat_id=chat_id,
        session_id=req.session_id,
        cache_owned=False,
        expires_at="",  # durable until the chat is deleted
        agent=getattr(sec, "agent", "") or "",
    )
    # No fn= here: the document pane appends it from the file's name
    # (baking it in too would duplicate the param).
    download_url = f"/v1/media/{download_token}?download=1"

    # Push to permission queue → stream pump → WS
    queue = get_permission_queue(queue_session_id)
    await queue.put({
        "event_type": wire.DOCUMENT_PREVIEW,
        "wopi_url": wopi_url,
        # Live only: the document pane posts it into its frame; the stored row
        # drops it (stream_pump._serialize_turn_rows) and a later load mints.
        "access_token": wopi_token,
        "access_token_ttl": token_ttl,
        "filename": filename,
        "file_id": file_id,
        "download_url": download_url,
        "snapshot_id": snapshot_id,
        "generation": generation,
        "version": version,
    })

    logger.info(f"Hook document-preview: session={req.session_id}, file={filename}")
    return {"status": "ok", "wopi_url": wopi_url, "file_id": file_id}
