"""The document pane's downloads: a version as it is, and a pane file as PDF
or CSV (the menu is the dashboard's ``DocumentPaneMenu.tsx``).

The live file as it is keeps the push's own link (``download_url``, a media
token). Every other download of the pane's menu is prepared here and served
by the platform's one download door, ``GET /v1/media/{token}``: the proof is
the pane's (``wopi.live_document``, ``wopi.snapshot_document``: the person's
access to the chat and a file the chat's pane holds), the file is read from
its checked descriptor beneath its root (``wopi.read_root``), a conversion
is file-tools' rendering engine behind its ``POST /convert`` (the proxy
never converts), and the result is a cache-owned file in the host media
cache with a ``media_tokens`` row of kind ``converted`` that expires after
``LINK_TTL_S`` (the media sweep removes both). ``serve_media`` names such a
row by its file, so the link's ``fn`` never reaches the header.

file-tools verifies the one-shot token of each conversion with one call
back here (``/v1/internal/convert/verify``) before it reads the body, the
render token's shape: a sibling container gains nothing from its route.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from api.media import wopi
from auth.providers import UserContext, get_current_user, require_auth
from services.infra import pinned_http, safe_fs
from services.media import media_pipeline
from storage import database as task_store
from storage.pg import run_db

logger = logging.getLogger("otodock.media")
router = APIRouter()

PDF, CSV = "pdf", "csv"
_WORD = frozenset({".docx", ".docm", ".dotx", ".dotm", ".doc", ".odt", ".rtf"})
_EXCEL = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".ods"})
_SLIDES = frozenset({".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".ppt", ".odp"})
MIMES = {PDF: "application/pdf", CSV: "text/csv; charset=utf-8"}

KIND = "converted"
# The file tools' own source cap (pdf._MAX_SOURCE_BYTES) and answer cap:
# file-tools refuses past them, and this side bounds what it sends and
# writes all the same.
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_ANSWER_BYTES = 128 * 1024 * 1024
# The whole request stays under a gateway's 100 s (Cloudflare answers 524).
CALL_TIMEOUT_S = 75.0
SLOT_WAIT_S = 5.0
SLOTS = 2
TOKEN_TTL_S = 90.0
LINK_TTL_S = 600
MAX_STANDING = 16
_CHUNK = 1024 * 1024
_NAME_MAX_BYTES = 200
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")

# In this process only: a conversion is seconds long, and a link minted
# before a restart is a row and a file the media sweep removes.
_tokens: dict[str, float] = {}
_busy: set[str] = set()
_running = 0
_latest: dict[str, Path] = {}


def targets_for(filename: str) -> tuple[str, ...]:
    """The conversions the pane's menu offers for a document, by its
    extension: the one owner of the list (the listing carries it, the
    route refuses any other)."""
    ext = PurePosixPath(filename or "").suffix.lower()
    if ext in _EXCEL:
        return (PDF, CSV)
    if ext in _WORD or ext in _SLIDES:
        return (PDF,)
    return ()


def download_name(filename: str, fmt: str) -> str:
    """The name a download is saved under: the document's own name, or its
    stem with the target's extension, cleaned of control characters and
    separators and kept under ``_NAME_MAX_BYTES``."""
    leaf = PurePosixPath((filename or "").replace("\\", "/")).name
    stem, suffix = (PurePosixPath(leaf).stem, f".{fmt}") if fmt else (leaf, "")
    stem = _UNSAFE_NAME.sub("", stem).strip(" .") or "document"
    budget = _NAME_MAX_BYTES - len(suffix.encode())
    while len(stem.encode()) > budget:
        stem = stem[:-1]
    return stem + suffix


def converted_root() -> Path:
    return media_pipeline.host_cache_dir() / media_pipeline.CONVERTED_DIR


class DownloadRequest(BaseModel):
    chat_id: str
    file_id: str
    snapshot_id: str = ""
    format: str = ""


@dataclass
class _Source:
    filename: str
    root: Path
    rel: str
    agent: str


def _source(u: UserContext, chat_id: str, file_id: str, snapshot_id: str) -> _Source:
    """The file a pane download reads, behind the pane's own proof. Store
    reads and path probes: run it through ``run_db``; its ``HTTPException``
    reaches the client unchanged."""
    if snapshot_id:
        chat, event, owner = wopi.snapshot_document(u, chat_id, snapshot_id)
        if (event.get("file_id") or "") != file_id:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        rel_path = wopi.snapshot_rel_path(owner, snapshot_id)
    else:
        chat, _owner, event, rel_path, _full = wopi.live_document(u, chat_id, file_id)
    root, rel = wopi.read_root(rel_path)
    filename = PurePosixPath(event.get("filename") or rel.rsplit("/", 1)[-1]).name
    return _Source(filename=filename, root=Path(root), rel=rel, agent=chat.get("agent") or "")


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mint_token() -> str:
    now = time.monotonic()
    for key, until in list(_tokens.items()):
        if until < now:
            _tokens.pop(key, None)
    token = secrets.token_urlsafe(32)
    _tokens[_token_key(token)] = now + TOKEN_TTL_S
    return token


@router.get("/v1/internal/convert/verify")
async def verify_convert_token(request: Request) -> Response:
    """file-tools asks whether the bearer is a live conversion token before
    it reads the body: 204 once, then 401, so a token copied from a log is
    spent."""
    token = wopi.request_wopi_token(request)
    until = _tokens.pop(_token_key(token), None) if token else None
    if until is None or until < time.monotonic():
        raise HTTPException(status_code=401, detail="not a live conversion token")
    return Response(status_code=204)


async def _take_slot() -> None:
    global _running
    give_up = time.monotonic() + SLOT_WAIT_S
    while _running >= SLOTS:
        if time.monotonic() >= give_up:
            raise HTTPException(status_code=503, detail="The server is busy. Try again in a moment.")
        await asyncio.sleep(0.1)
    _running += 1


def _release_slot() -> None:
    global _running
    _running = max(0, _running - 1)


@router.post("/v1/documents/download")
async def prepare_download(req: DownloadRequest, user: UserContext = Depends(get_current_user)):
    """Prepare a download of the pane's menu: ``format`` "pdf" or "csv" (a
    conversion of the live file or of the version ``snapshot_id``), or ""
    for a version as it is. Answers ``{download_url, filename}``: a media
    link that lives ``LINK_TTL_S``."""
    u = require_auth(user)
    fmt = (req.format or "").strip().lower()
    src = await run_db(_source, u, req.chat_id, req.file_id, req.snapshot_id)
    if fmt and fmt not in targets_for(src.filename):
        raise HTTPException(status_code=400, detail=f"This file cannot be downloaded as {fmt.upper()}.")
    if not fmt and not req.snapshot_id:
        raise HTTPException(status_code=400, detail="The live file downloads through its own link.")
    if u.sub in _busy:
        raise HTTPException(status_code=429, detail="A download is already being prepared. Wait for it to finish.")
    _busy.add(u.sub)
    try:
        await _take_slot()
        try:
            return await _prepare(u, req, src, fmt)
        finally:
            _release_slot()
    finally:
        _busy.discard(u.sub)


async def _prepare(u: UserContext, req: DownloadRequest, src: _Source, fmt: str) -> dict:
    try:
        fd, st = await asyncio.to_thread(safe_fs.open_regular_for_read, src.root, src.rel)
    except FileNotFoundError as err:
        raise HTTPException(status_code=404, detail="This file is no longer there.") from err
    except OSError as err:
        logger.warning("pane download of %s refused: %s", src.rel, type(err).__name__)
        raise HTTPException(status_code=404, detail="This file is no longer there.") from err
    try:
        if fmt and st.st_size > MAX_INPUT_BYTES:
            raise HTTPException(status_code=413, detail="This file is too large to convert here.")
        if not fmt and st.st_size > MAX_ANSWER_BYTES:
            raise HTTPException(status_code=413, detail="This file is too large to download here.")
        await asyncio.to_thread(_prune)
        token = secrets.token_urlsafe(32)
        folder = converted_root() / token
        name = download_name(src.filename, fmt)
        target = folder / name
        await asyncio.to_thread(folder.mkdir, 0o700, True)
        try:
            if fmt:
                await _convert(fd, PurePosixPath(src.filename).suffix.lower(), fmt, target)
                mime = MIMES[fmt]
            else:
                await asyncio.to_thread(_copy_fd, fd, target)
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            expires = (datetime.now(timezone.utc) + timedelta(seconds=LINK_TTL_S)).isoformat()
            await run_db(
                task_store.create_media_token, token, str(target),
                mime=mime, media_kind=KIND, chat_id=req.chat_id, cache_owned=True,
                expires_at=expires, owner_sub=u.sub, agent=src.agent,
            )
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, folder, True)
            raise
    finally:
        os.close(fd)
    previous = _latest.pop(u.sub, None)
    _latest[u.sub] = folder
    if previous is not None:
        await asyncio.to_thread(shutil.rmtree, previous, True)
    return {"download_url": f"/v1/media/{token}?download=1", "filename": name}


def _copy_fd(fd: int, target: Path) -> None:
    """The bytes of the checked descriptor into ``target``. Blocking."""
    with open(target, "xb") as out:
        while chunk := os.read(fd, _CHUNK):
            out.write(chunk)


def _prune() -> None:
    """At most ``MAX_STANDING`` prepared downloads stay (the oldest go
    first); their expiry is the media sweep's (``sweep_expired_media_tokens``
    and ``sweep_host_media_cache``). Blocking."""
    root = converted_root()
    try:
        entries = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    except FileNotFoundError:
        return
    for p in entries[: max(0, len(entries) - MAX_STANDING + 1)]:
        shutil.rmtree(p, True)


async def _chunks(fd: int) -> AsyncIterator[bytes]:
    sent = 0
    while chunk := await asyncio.to_thread(os.read, fd, _CHUNK):
        sent += len(chunk)
        if sent > MAX_INPUT_BYTES:
            raise HTTPException(status_code=413, detail="This file is too large to convert here.")
        yield chunk


async def _convert(fd: int, ext: str, fmt: str, target: Path) -> None:
    """file-tools' ``POST /convert`` with the descriptor's bytes; its answer
    written to ``target``. Every failure is the person's sentence."""
    from services.apps import app_render
    base = await asyncio.to_thread(app_render.renderer_base_url)
    if not base:
        raise HTTPException(status_code=503, detail="Converting documents is not available on this install.")
    word = fmt.upper()
    with app_render._held():
        await _convert_held(fd, ext, fmt, target, base, word)


async def _convert_held(fd: int, ext: str, fmt: str, target: Path, base: str, word: str) -> None:
    """The conversion while file-tools is held in use: woken first when it
    sleeps, never put to sleep under it."""
    from services.apps import app_render
    tool = app_render._renderer_tool()
    if tool is not None:
        from services.containers import door
        if await door.ready(tool):
            raise HTTPException(status_code=503, detail=f"The {word} could not be made right now. Try again in a moment.")
    bearer = _mint_token()
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            async with httpx.AsyncClient(**pinned_http.tool_transport(timeout=CALL_TIMEOUT_S)) as client:
                async with client.stream(
                    "POST", f"{base}/convert", params={"ext": ext.lstrip("."), "format": fmt},
                    content=_chunks(fd), headers={"Authorization": f"Bearer {bearer}"},
                ) as r:
                    if r.status_code == 200:
                        await _save_answer(r, target, word)
                        return
                    await r.aread()
                    reason = _reason(r)
    except TimeoutError as err:
        raise HTTPException(status_code=504, detail=f"Making the {word} took too long. Try again in a moment.") from err
    except httpx.TimeoutException as err:
        raise HTTPException(status_code=504, detail=f"Making the {word} took too long. Try again in a moment.") from err
    except httpx.HTTPError as err:
        logger.warning("file-tools did not answer a conversion: %s", type(err).__name__)
        raise HTTPException(status_code=503, detail=f"The {word} could not be made right now. Try again in a moment.") from err
    logger.info("file-tools refused a conversion to %s (%d): %s", fmt, r.status_code, reason)
    if r.status_code == 404:
        raise HTTPException(status_code=503, detail="This install needs an update to convert documents. Ask an admin.")
    if r.status_code == 413:
        raise HTTPException(status_code=413, detail="This file is too large to convert here.")
    if r.status_code == 504:
        raise HTTPException(status_code=504, detail=f"Making the {word} took too long. Try again in a moment.")
    raise HTTPException(status_code=502, detail=f"The {word} could not be made from this file.")


def _reason(r: httpx.Response) -> str:
    try:
        return str(r.json().get("reason") or "")[:300]
    except ValueError:
        return ""


async def _save_answer(r: httpx.Response, target: Path, word: str) -> None:
    written = 0
    out = await asyncio.to_thread(open, target, "xb")
    try:
        async for chunk in r.aiter_bytes(_CHUNK):
            written += len(chunk)
            if written > MAX_ANSWER_BYTES:
                raise HTTPException(status_code=413, detail=f"The {word} is too large to download here.")
            await asyncio.to_thread(out.write, chunk)
    finally:
        await asyncio.to_thread(out.close)
    if not written:
        raise HTTPException(status_code=502, detail=f"The {word} could not be made from this file.")


def forget() -> None:
    """Tests: the process-memory state back to empty."""
    global _running
    _tokens.clear()
    _busy.clear()
    _latest.clear()
    _running = 0
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(converted_root())
