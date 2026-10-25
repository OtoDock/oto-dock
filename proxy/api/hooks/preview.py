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

from api.hooks import paths, routing
from api.sessions.sessions import verify_session_match_async
from core.events.artifact_events import TOKEN_PERSON
from core.session.session_state import (
    _sessions,
    get_permission_queue,
    get_session_security,
)
from storage import database as task_store
from storage.pg import run_db
from ws import wire_events as wire

logger = logging.getLogger("otodock")
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


def note_next_generation(file_id: str, digest: str) -> int:
    """Note a new document generation for a file whose bytes changed (past
    the noted one and the time in ms), with the sha256 of its bytes now;
    returns it. The ``file-written`` hook notes it before it announces the
    write, so a load that mints on that ``file_updated`` already opens the
    document of the push that follows (which then reuses it: the file holds
    the noted bytes)."""
    generation = max(int(time.time() * 1000), pushed_generation(file_id) + 1)
    note_pushed_generation(file_id, generation, digest)
    return generation


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


def _person_claims(sub: str, tree_rel: str, host_cache: bool, agent: str,
                   session_person: str = "") -> tuple[str, str] | None:
    """``(user_name, permissions)`` of a pushed token minted for a person:
    the name a dashboard mint gives them and the live mint's write decision
    for their own role on the agent; None when ``sub`` has no users row (a
    delegated lane's session carries the source agent's slug when no person
    acts). Store reads: call it on the DB executor."""
    from api.media.wopi import live_permissions
    from auth.providers import acting_role_of
    user = task_store.get_user(sub)
    if not user:
        return None
    username = task_store.get_username_by_sub(sub) or ""
    name = user.get("name") or user.get("display_name") or username or "User"
    return name, live_permissions(tree_rel, acting_role_of(sub, agent), username, agent,
                                  host_cache=host_cache, session_person=session_person)


def _push_person(chat_id: str, queue_session_id: str) -> str:
    """The one person a push reaches, "" when none: its token is minted for
    them. A chat's pump turn runs as its ``wake_person``, and the pump's
    sockets hand the token to that person's connections only
    (``artifact_events.for_viewer``). A terminal chat has no pump: its
    session's drainer hands every push to the socket holding the prompt
    slot, which only the session's sole driver may hold
    (``InteractiveSession.sole_driver``). Nobody for a task run, a wake, a
    meeting, a terminal anyone may drive (a task chat), a terminal of
    another chat or already closing, or a session id both a pump and a
    terminal read (either may take the push). Loop-only reads, no await: the hook asks again in
    the step it queues the push."""
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session
    pump = _active_pumps.get(chat_id) if chat_id else None
    if pump is not None and (pump.is_done or pump.session_id != queue_session_id):
        pump = None
    terminal = interactive_session.get(queue_session_id)
    if terminal is not None and (not terminal.alive or terminal.closing):
        terminal = None
    if pump is not None:
        return "" if terminal is not None else pump.wake_person or ""
    if terminal is not None and chat_id and terminal.chat_id == chat_id:
        return terminal.sole_driver
    return ""


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
    from api.media.wopi import (
        build_cool_url,
        create_wopi_token,
        encode_file_id,
        encode_wopi_id,
    )

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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="File must be within agents directory") from exc

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

    session_meta = _sessions.get(req.session_id, {})
    sec = get_session_security(req.session_id)
    tree_rel = rel_path.partition("/")[2]  # strip "<agent>/" → agent-tree-relative
    # Host-cache doc (a Desktop/Downloads file pulled through THIS session's
    # cache): the agent-tree write matrix doesn't apply — the platform copy is
    # a mirror of a file on the session's own machine. Only the session's own
    # person edits it (_person_claims), PutFile pushing the bytes back to the
    # real file (wopi.wopi_put_file), where the satellite's own host write
    # policy is the next gate. Session-scoped: only THIS session's cache root
    # qualifies, so another session's cached files stay view-only here.
    _is_host_cache = False
    if sec is not None:
        from core.remote import remote_file_flow
        try:
            _is_host_cache = file_path.resolve().is_relative_to(
                remote_file_flow.host_cache_session_root(req.session_id).resolve()
            )
        except OSError:
            _is_host_cache = False

    # The chat the push lands in: its pane's saves refresh this push's
    # version, so the token names it.
    chat_id = await routing.resolve_hook_chat_id(req.session_id) or None

    # The token is minted only for the one person the push reaches
    # (_push_person, never the chat row's owner, which is the agent on a
    # shared chat): their name and their own role's write decision, so a
    # save from their pane reads "by <name>", records them as the file's
    # author and skips their own dashboards. With no person the push
    # carries no token and mints none: every viewer's pane mints its own
    # at its own role (/v1/documents/preview-wopi-url).
    queue_session_id = routing.resolve_hook_route(req.session_id).queue_session_id
    person = _push_person(chat_id or "", queue_session_id)
    claims: tuple[str, str] | None = None
    if person:
        claims = await run_db(
            _person_claims, person, tree_rel, _is_host_cache, getattr(sec, "agent", "") or "",
            getattr(sec, "username", "") or "",
        )
    wopi_token, token_ttl = "", 0
    if claims is not None:
        user_name, permissions = claims
        # Reuse a WOPI token while it is still valid and opens this push's
        # document (a token opens one document generation: a push that made
        # a new one mints, and its entry replaces the old). Keyed by
        # permission too, so a viewer never receives a cached edit token —
        # or vice-versa — for the same shared file_id, by chat, so a token
        # never carries another chat's claim, and by person.
        cache_key = (file_id, permissions, chat_id or "", person)
        cached = _wopi_token_cache.get(cache_key)
        if cached and cached["expires"] > now and cached["generation"] == document_generation:
            wopi_token, token_ttl = cached["token"], cached["ttl"]
        else:
            wopi_token, token_ttl = create_wopi_token(
                rel_path, person, user_name, permissions, session_meta.get("agent", ""),
                chat_id=chat_id or "", generation=document_generation,
            )
            for key in [k for k, v in _wopi_token_cache.items() if v["expires"] <= now]:
                del _wopi_token_cache[key]
            _wopi_token_cache[cache_key] = {
                "token": wopi_token, "ttl": token_ttl, "generation": document_generation,
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
            preview_snapshots.create_snapshot, chat_id, file_path,  # type: ignore[arg-type]  # mypy solves to_thread's return from the target's declared str through `or ""` (checker limitation)
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

    # Push to permission queue → stream pump (or the terminal's drainer) → WS
    item = {
        "event_type": wire.DOCUMENT_PREVIEW,
        "wopi_url": wopi_url,
        "filename": filename,
        "file_id": file_id,
        "download_url": download_url,
        "snapshot_id": snapshot_id,
        "generation": generation,
        "version": version,
    }
    # The person is read again in the loop step the push is queued: a
    # takeover while this hook awaited starts closing the terminal, and the
    # next person's re-warm reuses the session id and reads the queue under
    # it, so the token rides only while the push still reaches the person it
    # names. Live only: the document pane posts it into its frame; the
    # stored row drops it (stream_pump._serialize_turn_rows) and a later
    # load mints.
    if wopi_token and _push_person(chat_id or "", queue_session_id) == person:
        item["access_token"] = wopi_token
        item["access_token_ttl"] = token_ttl
        item[TOKEN_PERSON] = person
    get_permission_queue(queue_session_id).put_nowait(item)

    logger.info("Hook document-preview: session=%s, file=%s", req.session_id, filename)
    return {"status": "ok", "wopi_url": wopi_url, "file_id": file_id}
