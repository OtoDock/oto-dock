"""WOPI (Web Application Open Platform Interface) endpoints for Collabora Online.

Collabora uses WOPI to fetch/save files from the proxy. The dashboard requests
WOPI URLs to embed Collabora iframes for live document preview and editing.

Security: Every WOPI call is authenticated via a short-lived JWT access_token
that is scoped to a specific file, user, agent, and permission level.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config
from api.media.media import HOST_CACHE_SEGMENT, FdFileResponse
from auth.providers import get_current_user, require_auth, UserContext
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path, resolve_under, safe_agent_dir
from auth import roles
from core import layout
from storage.pg import run_db

logger = logging.getLogger("claude-proxy")
router = APIRouter(tags=["wopi"])

# ---------------------------------------------------------------------------
# WOPI Token Management
# ---------------------------------------------------------------------------

_WOPI_TOKEN_TTL = 14400  # 4 hours

# file_path namespace for version-pinned preview snapshots
# (services/media/preview_snapshots.py): "<ns>/<chat_id>/<snapshot_id>".
# The leading dot keeps it disjoint from every agent slug, so a snapshot
# file_id can never collide with (or traverse into) an agent tree.
_SNAPSHOT_NS = ".preview-snapshots"


def encode_file_id(relative_path: str) -> str:
    """Base64url-encode a relative file path for use as WOPI file_id."""
    return base64.urlsafe_b64encode(relative_path.encode()).decode().rstrip("=")


def decode_file_id(file_id: str) -> str:
    """Decode base64url file_id back to a relative file path."""
    padded = file_id + "=" * (4 - len(file_id) % 4)
    return base64.urlsafe_b64decode(padded).decode()


# Collabora keys a loaded document by its WOPISrc path, so two ids of one
# file are two documents: a load after a new push of the file gets a fresh
# document read from the stored bytes, while the loads after one push share
# a document (co-editing kept). The live loads of a file (the pane's, the
# workspace tab's) carry its newest push's document generation after a
# newline. The bare id (the path alone) is every chat-level record's (the
# event, the listing, the versions, the dashboard's file id, the
# file_updated match) and the frame of a file no push was noted for.
_GENERATION_RE = re.compile(r"[0-9]{1,20}")


def encode_wopi_id(relative_path: str, generation: int = 0) -> str:
    """The WOPI file id of a path, keyed by a push ``generation`` when one
    is given, zero being the bare id."""
    return encode_file_id(f"{relative_path}\n{generation}" if generation else relative_path)


def decode_wopi_id(file_id: str) -> tuple[str, int]:
    """``(path, generation)`` of a WOPI file id: a text with no newline is a
    bare id (generation 0), any other names the part before its LAST
    newline, the part after it being 1 to 20 ASCII digits. The routes then
    compare the path by equality with the token's ``file_path``, so a token
    opens exactly its own path, bare or keyed, and nothing beneath it.
    ``ValueError`` on an id that does not decode or whose suffix is not a
    generation."""
    try:
        raw = decode_file_id(file_id)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("undecodable file id") from exc
    path, sep, suffix = raw.rpartition("\n")
    if not sep:
        return raw, 0
    if not _GENERATION_RE.fullmatch(suffix):
        raise ValueError("not a generation-keyed file id")
    return path, int(suffix)


def snapshot_rel_path(chat_id: str, snapshot_id: str) -> str:
    """The token file_path / file_id payload for a preview snapshot."""
    return f"{_SNAPSHOT_NS}/{chat_id}/{snapshot_id}"


def create_wopi_token(
    file_path: str,
    user_sub: str,
    user_name: str,
    permissions: str,
    agent: str,
    display_name: str = "",
    chat_id: str = "",
) -> tuple[str, int]:
    """Create a JWT WOPI access token. Returns (token, expiry_ms).

    ``display_name`` overrides CheckFileInfo's BaseFileName — snapshot files
    are stored under opaque extension-less ids, and Collabora picks its
    renderer from the BaseFileName extension, so snapshot tokens must carry
    the original filename. ``chat_id`` names the chat whose document pane
    the token opens the file in: a save with it refreshes the file's newest
    version there."""
    now = int(time.time())
    exp = now + _WOPI_TOKEN_TTL
    payload = {
        # Purpose discriminator: WOPI_SECRET defaults to JWT_SECRET, which
        # also signs session/audio/broker tokens — without this claim the
        # verifier below would accept ANY platform JWT whose claim shape
        # happens to fit (and vice versa).
        "purpose": "wopi",
        "file_path": file_path,
        "user_sub": user_sub,
        "user_name": user_name,
        "permissions": permissions,  # "view" or "edit"
        "agent": agent,
        "iat": now,
        "exp": exp,
    }
    if display_name:
        payload["display_name"] = display_name
    if chat_id:
        payload["chat_id"] = chat_id
    token = jwt.encode(payload, config.WOPI_SECRET, algorithm="HS256")
    return token, exp * 1000  # expiry in milliseconds for Collabora


def validate_wopi_token(token: str) -> dict | None:
    """Validate and decode a WOPI access token. Returns claims or None."""
    try:
        claims = jwt.decode(token, config.WOPI_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if claims.get("purpose") != "wopi":
        return None
    return claims


class _Unreadable(Exception):
    """The file a save check reads is there but could not be read."""


def _open_wopi_regular(file_id: str, claims: dict, *,
                       unreadable: type[Exception] | None = None) -> tuple[int, os.stat_result, str]:
    """``(fd, stat, name)`` of the regular file a token names, for the read
    routes. The signed ``file_path`` is the binding: it is the path the mint
    resolved and authorized, and it is opened beneath its root through
    ``safe_fs`` with no symlink followed, so a file swapped for a link after
    the mint is "not found" whatever it points at (no role is re-checked:
    the three mint rules differ and the path is already theirs). Blocking:
    run it in a thread. The caller closes the descriptor. ``unreadable``,
    when given, is raised instead of the 404 for a file that is there but
    could not be opened (the save check fails open on it)."""
    try:
        rel_path, _generation = decode_wopi_id(file_id)
    except ValueError:
        raise HTTPException(status_code=403, detail="Token/file mismatch")
    if claims.get("file_path") != rel_path:
        raise HTTPException(status_code=403, detail="Token/file mismatch")
    if rel_path.startswith(_SNAPSHOT_NS + "/"):
        from services.media import preview_snapshots
        parts = rel_path.split("/")
        if len(parts) != 3 or not all(preview_snapshots._valid_id(x) for x in parts[1:]):
            raise HTTPException(status_code=404, detail="Snapshot not found")
        root, rel = config.PREVIEW_SNAPSHOT_DIR, f"{parts[1]}/{parts[2]}"
    else:
        first, _, rest = rel_path.partition("/")
        if not rest or (first != HOST_CACHE_SEGMENT and not config.is_safe_agent_name(first)):
            raise HTTPException(status_code=404, detail="File not found")
        root, rel = config.AGENTS_DIR, rel_path
    try:
        fd, st = safe_fs.open_regular_for_read(root, rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except OSError as exc:
        logger.warning("WOPI read refused for %s: %s", rel_path, type(exc).__name__)
        if unreadable is not None:
            raise unreadable(type(exc).__name__) from exc
        raise HTTPException(status_code=404, detail="File not found")
    return fd, st, rel.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# WOPI Lock Management (in-memory, for conflict prevention)
# ---------------------------------------------------------------------------

_wopi_locks: dict[str, dict] = {}  # file_id → {"lock_id": str, "expires": float}
# Raw ids: one lock per generation of a file, so the table is bounded (the
# expired entries go first, then the oldest).
_WOPI_LOCKS_MAX = 4096


def _get_lock(file_id: str) -> str | None:
    """Get current lock for a file, or None if unlocked/expired."""
    info = _wopi_locks.get(file_id)
    if not info:
        return None
    if time.time() > info["expires"]:
        _wopi_locks.pop(file_id, None)
        return None
    return info["lock_id"]


def _set_lock(file_id: str, lock_id: str):
    now = time.time()
    _wopi_locks[file_id] = {"lock_id": lock_id, "expires": now + _WOPI_TOKEN_TTL}
    if len(_wopi_locks) > _WOPI_LOCKS_MAX:
        for key in [k for k, v in _wopi_locks.items() if v["expires"] <= now]:
            del _wopi_locks[key]
        while len(_wopi_locks) > _WOPI_LOCKS_MAX:
            del _wopi_locks[next(iter(_wopi_locks))]


def _remove_lock(file_id: str, lock_id: str) -> bool:
    current = _get_lock(file_id)
    if current == lock_id:
        _wopi_locks.pop(file_id, None)
        return True
    return False


def _post_message_origin() -> str:
    """Origin Collabora targets when posting status (e.g. ``Doc_ModifiedStatus``)
    to the embedding dashboard frame — the WOPI ``PostMessageOrigin`` field.

    Returns the ``scheme://host[:port]`` of the dashboard's public URL so the
    dashboard can read the doc's modified state for the reload dirty-guard.
    Falls back to ``"*"`` only if ``DASHBOARD_PUBLIC_URL`` is unconfigured."""
    raw = (getattr(config, "DASHBOARD_PUBLIC_URL", "") or "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            p = urllib.parse.urlparse(raw)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}"
    return "*"


# ---------------------------------------------------------------------------
# WOPI Endpoints (called by Collabora)
# ---------------------------------------------------------------------------

def _last_modified(mtime: float) -> str:
    """The ``LastModifiedTime`` text: ISO 8601 UTC at whole seconds.
    Collabora keeps the text it saw last and sends it back on its next
    PutFile as ``X-COOL-WOPI-Timestamp``."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(mtime))


# The LastModifiedTime answered per file id, with the size, mtime and hash
# of the bytes it was answered for. A file rewritten with the same bytes (a
# satellite pull, a fan-out echo, a no-op copy) gets a new mtime without
# changing; answering that mtime would make Collabora report a change and
# the save check below refuse a good save, so the same bytes keep their
# text. Files past the hash cap always answer their own mtime.
_STABLE_TIMES_MAX = 4096
_HASH_MAX_BYTES = 100 * 1024 * 1024
_stable_times: OrderedDict[str, tuple[str, int, int, str]] = OrderedDict()
_stable_times_lock = threading.Lock()  # the worker threads of the WOPI routes share it


def _sha256_fd(fd: int) -> str:
    h = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, offset)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)
        offset += len(chunk)


def _stable_last_modified(file_id: str, fd: int, st: os.stat_result) -> tuple[str, str | None]:
    """``(LastModifiedTime, sha256)`` this host answers for the file open at
    ``fd`` (CheckFileInfo, PutFile's answer and the save check share it):
    the recorded text while the bytes are the ones it was answered for, else
    the file's own mtime, recorded. The hash is None past the cap. Blocking:
    a worker-thread call."""
    with _stable_times_lock:
        entry = _stable_times.get(file_id)
        if entry is not None and entry[1] == st.st_size and entry[2] == st.st_mtime_ns:
            _stable_times.move_to_end(file_id)
            return entry[0], entry[3]
        if st.st_size > _HASH_MAX_BYTES:
            _stable_times.pop(file_id, None)
            return _last_modified(st.st_mtime), None
    digest = _sha256_fd(fd)
    text = entry[0] if entry is not None and entry[1] == st.st_size and entry[3] == digest \
        else _last_modified(st.st_mtime)
    with _stable_times_lock:
        _stable_times[file_id] = (text, st.st_size, st.st_mtime_ns, digest)
        _stable_times.move_to_end(file_id)
        while len(_stable_times) > _STABLE_TIMES_MAX:
            _stable_times.popitem(last=False)
    return text, digest


def _answer_seconds(text: str) -> int | None:
    """Whole UTC seconds of a LastModifiedTime text (this host's or the one
    Collabora sends back): ISO 8601 with an optional fraction and a ``Z`` or
    an offset; a text with no offset reads as UTC. None when unreadable."""
    try:
        dt = datetime.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() // 1)


# The hash of the bytes each open document was built from, per file id:
# recorded when Collabora fetches the file (GetFile) and when a save lands.
# A view that joins an open document answers its CheckFileInfo with the
# file's current time, and Collabora sends that time back on its next save,
# so after a write the timestamp alone stops telling once another view has
# joined (a second device, a teammate). A save whose file no longer holds
# these bytes is refused too. Gone on a restart: the timestamp check stays.
_doc_bases: OrderedDict[str, str] = OrderedDict()


def _note_doc_base(file_id: str, digest: str | None) -> None:
    with _stable_times_lock:
        if digest is None:
            _doc_bases.pop(file_id, None)
            return
        _doc_bases[file_id] = digest
        _doc_bases.move_to_end(file_id)
        while len(_doc_bases) > _STABLE_TIMES_MAX:
            _doc_bases.popitem(last=False)


def _doc_base(file_id: str) -> str | None:
    with _stable_times_lock:
        return _doc_bases.get(file_id)


def _note_saved_base(file_id: str, body: bytes) -> str | None:
    """A landed save: its bytes are the document's base now; returns their
    hash (None past the cap). Blocking (the hash): a worker-thread call."""
    digest = hashlib.sha256(body).hexdigest() if len(body) <= _HASH_MAX_BYTES else None
    _note_doc_base(file_id, digest)
    return digest


_bad_timestamp_warned: OrderedDict[str, None] = OrderedDict()


def _warn_bad_timestamp(file_id: str, header: str) -> None:
    if file_id in _bad_timestamp_warned:
        return
    _bad_timestamp_warned[file_id] = None
    while len(_bad_timestamp_warned) > 512:
        _bad_timestamp_warned.popitem(last=False)
    logger.warning("WOPI PutFile: unreadable X-COOL-WOPI-Timestamp %r, saved without the check", header[:64])


_Answer = tuple[str, os.stat_result, str | None]  # (LastModifiedTime, stat, sha256)


def _current_answer(file_id: str, claims: dict) -> _Answer | None:
    """The answer, stat and hash of the file a token names as it is now,
    None when it is gone; ``_Unreadable`` when it is there but could not be
    read. Blocking: a worker-thread call."""
    try:
        fd, st, _name = _open_wopi_regular(file_id, claims, unreadable=_Unreadable)
    except HTTPException:
        return None
    try:
        text, digest = _stable_last_modified(file_id, fd, st)
        return text, st, digest
    finally:
        os.close(fd)


def _saved_answer(file_id: str, claims: dict) -> _Answer | None:
    """The file's answer after a save, None when it cannot be read.
    Blocking: a worker-thread call."""
    try:
        return _current_answer(file_id, claims)
    except _Unreadable:
        return None


def _put_file_answer(saved: _Answer | None) -> Response:
    """PutFile's 200 carries the saved file's ``LastModifiedTime`` (without
    it Collabora logs a warning on every save), the text CheckFileInfo
    answers for the same bytes. A file gone between the write and the stat
    answers a plain 200, the save already happened (Collabora then stops
    sending the timestamp for that session)."""
    if saved is None:
        return Response(status_code=200)
    return JSONResponse({"LastModifiedTime": saved[0]})


def _refresh_version(claims: dict, file_id: str, body: bytes, digest: str | None,
                     saved: _Answer | None, before: _Answer | None) -> None:
    """After a save that passed the check with a token from a chat's
    document pane: queue the refresh of the file's newest version in that
    chat (``preview_snapshots.refresh_newest``) on the chat's writer lane,
    after the turn rows already queued there. A forced overwrite (no
    timestamp) refreshes nothing, so the agent's delivery stays its
    version; so does a save another write replaced before the stat."""
    chat_id = claims.get("chat_id") or ""
    if not chat_id or before is None or saved is None or saved[1].st_size != len(body):
        return
    if digest is not None and saved[2] is not None and saved[2] != digest:
        return
    import functools
    from core.events import chat_writer, stream_pump
    from services.media import preview_snapshots
    chat_writer.submit(chat_id, functools.partial(
        preview_snapshots.refresh_newest, chat_id, file_id,
        stream_pump.pending_preview_snapshot(chat_id, file_id), body,
        saved[1].st_mtime_ns, (before[1].st_size, before[1].st_mtime_ns, before[2]),
    ), label="preview_version_refresh")


def _conflict(file_id: str, header: str, current: _Answer | None,
              request: Request, rel: str) -> Response | None:
    """The save check: a save whose ``X-COOL-WOPI-Timestamp`` names another
    second than the file's current answer, whose file no longer holds the
    bytes its document was built from, or whose file is gone since, is
    answered 409 ``{"COOLStatusCode": 1010}`` and writes nothing, so
    Collabora asks the person to discard their edits or overwrite. None
    lets the save go."""
    header_seconds = _answer_seconds(header)
    if header_seconds is None:
        _warn_bad_timestamp(file_id, header)
        if current is None:
            return None
    if current is not None:
        same_time = header_seconds is None or _answer_seconds(current[0]) == header_seconds
        base = _doc_base(file_id)
        same_bytes = base is None or current[2] is None or current[2] == base
        if same_time and same_bytes:
            return None
    if request.headers.get("X-COOL-WOPI-IsExitSave", "").lower() == "true":
        # The last editor has left: nobody can answer the dialog, so these
        # edits are lost.
        logger.warning(
            "WOPI PutFile refused: %s changed since it was loaded; the closing "
            "editor's save (%s bytes) is dropped",
            rel.rsplit("/", 1)[-1], request.headers.get("content-length", "?"),
        )
    elif _refusal_news(file_id):
        # A view of an earlier version keeps retrying while its dialog is
        # open (every few seconds): one line a minute per document.
        logger.info("WOPI PutFile refused: %s changed since it was loaded", rel.rsplit("/", 1)[-1])
    return JSONResponse({"COOLStatusCode": 1010}, status_code=409)


_refusals_logged: OrderedDict[str, float] = OrderedDict()
_REFUSAL_LOG_EVERY_S = 60.0


def _refusal_news(file_id: str) -> bool:
    """True when a refused save of this document is worth a line now."""
    now = time.monotonic()
    last = _refusals_logged.get(file_id)
    if last is not None and now - last < _REFUSAL_LOG_EVERY_S:
        return False
    _refusals_logged[file_id] = now
    _refusals_logged.move_to_end(file_id)
    while len(_refusals_logged) > 512:
        _refusals_logged.popitem(last=False)
    return True


def _file_info(file_id: str, claims: dict) -> tuple[os.stat_result, str, str]:
    """``(stat, name, LastModifiedTime)`` for CheckFileInfo, from the checked
    descriptor. Blocking: a worker-thread call."""
    fd, st, name = _open_wopi_regular(file_id, claims)
    try:
        return st, name, _stable_last_modified(file_id, fd, st)[0]
    finally:
        os.close(fd)


def _open_for_get(file_id: str, claims: dict) -> tuple[int, os.stat_result]:
    """The checked descriptor GetFile streams, with the bytes it holds noted
    as the document's base (hashed from the same descriptor, so a write
    landing meanwhile is not mistaken for them). Blocking: a worker-thread
    call."""
    fd, st, _name = _open_wopi_regular(file_id, claims)
    try:
        _note_doc_base(file_id, _stable_last_modified(file_id, fd, st)[1])
    except BaseException:
        os.close(fd)
        raise
    return fd, st


@router.get("/wopi/files/{file_id}")
async def wopi_check_file_info(
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI CheckFileInfo — returns file metadata for Collabora."""
    claims = validate_wopi_token(access_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired WOPI token")
    st, name, last_modified = await asyncio.to_thread(_file_info, file_id, claims)

    can_write = claims.get("permissions") == "edit"

    return {
        # Snapshot tokens carry display_name (opaque on-disk id, no
        # extension) — Collabora picks its renderer from this extension.
        "BaseFileName": claims.get("display_name") or name,
        "Size": st.st_size,
        "OwnerId": claims.get("user_sub", ""),
        "UserId": claims.get("user_sub", ""),
        "UserFriendlyName": claims.get("user_name", "User"),
        "UserCanWrite": can_write,
        "UserCanNotWriteRelative": True,
        "SupportsLocks": True,
        "SupportsUpdate": can_write,
        "LastModifiedTime": last_modified,
        # Co-edit presence: show co-editors' names/cursors and their
        # join/leave messages so two users editing the same doc see each other.
        "HideUserList": "false",
        "DisableInactiveMessages": "false",
        "HidePrintOption": True,
        # Enable Collabora→host postMessage so the dashboard can read the doc's
        # modified state and only auto-reload on an external change when there
        # are no unsaved edits (dirty-guard).
        "PostMessageOrigin": _post_message_origin(),
    }


@router.get("/wopi/files/{file_id}/contents")
async def wopi_get_file(
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI GetFile — returns raw file bytes."""
    claims = validate_wopi_token(access_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired WOPI token")
    fd, st = await asyncio.to_thread(_open_for_get, file_id, claims)
    # Streamed from the checked descriptor: Collabora re-fetches on every
    # retry, and holding a large document (a 536MB preview, once) fully in
    # proxy memory per fetch is what a range-capable FileResponse exists to
    # avoid.
    try:
        return FdFileResponse(
            fd, st,
            media_type="application/octet-stream",
            headers={"X-WOPI-ItemVersion": str(int(st.st_mtime))},
        )
    except BaseException:
        os.close(fd)
        raise


@router.post("/wopi/files/{file_id}/contents")
async def wopi_put_file(
    request: Request,
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI PutFile: saves file from Collabora (user editing).

    The signed ``file_path`` is the binding (the mint resolved and authorized
    it): it must name an agent tree file (``<agent>/<canonical rel>``) or a
    host-cache mirror (``.remote-host-cache/<session>/<digest>/<name>``), and
    every write below opens that rel beneath ``AGENTS_DIR`` through
    ``safe_fs`` in a worker thread, no component followed; a token whose path
    fits neither shape is refused, never written."""
    claims = validate_wopi_token(access_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired WOPI token")
    try:
        rel, _generation = decode_wopi_id(file_id)
    except ValueError:
        raise HTTPException(status_code=403, detail="Token/file mismatch")
    if claims.get("file_path") != rel:
        raise HTTPException(status_code=403, detail="Token/file mismatch")
    # The lock, the answered time and the document's base are this
    # document's (the raw id, one per generation). The version a save
    # refreshes is the chat's file (the bare id).
    bare_id = encode_file_id(rel)

    if claims.get("permissions") != "edit":
        raise HTTPException(status_code=403, detail="Write permission required")
    # Snapshots are immutable history; tokens for them are minted view-only,
    # but refuse writes on the namespace itself as defence in depth.
    if rel.startswith(_SNAPSHOT_NS + "/"):
        raise HTTPException(status_code=403, detail="Snapshots are read-only")
    agent_slug, _, tree_rel = rel.partition("/")
    try:
        if not tree_rel or normalize_rel_path(tree_rel) != tree_rel:
            raise PathOutsideRoot(rel)
        if agent_slug != HOST_CACHE_SEGMENT and not config.is_safe_agent_name(agent_slug):
            raise PathOutsideRoot(rel)
    except PathOutsideRoot:
        raise HTTPException(status_code=403, detail="Token/file mismatch")

    # Check lock (if locked by someone else, reject)
    lock_header = request.headers.get("X-WOPI-Lock", "")
    current_lock = _get_lock(file_id)
    if current_lock and lock_header != current_lock:
        return Response(
            status_code=409,
            headers={"X-WOPI-Lock": current_lock},
        )

    body = await request.body()
    name = rel.rsplit("/", 1)[-1]
    # Collabora sends back the LastModifiedTime it saw last; a forced
    # overwrite after a refusal comes without it and saves as before.
    timestamp = request.headers.get("X-COOL-WOPI-Timestamp")

    before: _Answer | None = None
    if agent_slug == HOST_CACHE_SEGMENT:
        if timestamp is not None:
            # Compared with the cache copy the save replaces; an edit made
            # on the machine since the last pull is not seen here.
            try:
                before = await asyncio.to_thread(_current_answer, file_id, claims)
            except _Unreadable:
                pass  # not comparable: the save goes on (logged above)
            else:
                refused = _conflict(file_id, timestamp, before, request, rel)
                if refused is not None:
                    return refused
        # Host-cache doc: the platform copy is a MIRROR of a file on the
        # session's own machine (Desktop/Downloads pulled through the lazy
        # cache). Persist the cache copy atomically, then push the bytes back
        # to the REAL file via the sidecar's (machine, abs_path) — the
        # satellite's host write policy is the authoritative gate there. If
        # the machine refuses or is offline, FAIL the save (Collabora shows
        # a save error) instead of letting cache and reality silently
        # diverge. Never routed through propagate_write: host files have no
        # agent-tree fan-out.
        session_id = tree_rel.split("/")[0]
        try:
            prev = await asyncio.to_thread(_host_cache_write, rel, body)
        except OSError as exc:
            logger.warning("WOPI PutFile (host-cache) refused for %s: %s", rel, type(exc).__name__)
            raise HTTPException(status_code=403, detail="Token/file mismatch")
        from core.remote import remote_file_flow
        ok = await remote_file_flow.push_back_host_path(session_id, str(config.AGENTS_DIR / rel))
        if not ok:
            # The cache MIRRORS the machine — a failed push must not leave
            # diverged bytes that later reads would serve as truth. Restore
            # the pre-save content (best-effort) and fail the save.
            if prev is not None:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(_host_cache_write, rel, prev)
            raise HTTPException(
                status_code=500,
                detail="Could not write the file back to the remote machine "
                       "(offline or refused) — the save was not applied there.",
            )
        logger.info(
            "WOPI PutFile (host-cache → machine): %s (%d bytes) by %s",
            name, len(body), claims.get("user_name"),
        )
        digest = await asyncio.to_thread(_note_saved_base, file_id, body)
        saved = await asyncio.to_thread(_saved_answer, file_id, claims)
        _refresh_version(claims, bare_id, body, digest, saved, before)
        return _put_file_answer(saved)

    # Persist + propagate. ``propagate_write`` does the authoritative
    # atomic write to the platform agent tree AND fans the saved bytes out to
    # every machine running a live session of this agent, all under the global
    # per-(agent, rel_path) lock, so a concurrent satellite / file-tools write
    # can't interleave (disk and satellites converge on the same bytes).
    # Collabora already live-merged concurrent human editors, so ``body`` IS the
    # merged result → no conflict capture (see propagate_write). agent_slug +
    # agent-tree rel come from the token's AGENTS_DIR-relative file_path
    # ("<agent>/workspace/..." or "<agent>/users/{u}/...").
    from services.remote import workspace_fanout
    from storage import database as _db
    _sub = claims.get("user_sub", "")
    _writer = await run_db(_db.get_username_by_sub, _sub) if _sub else None
    precheck: Callable[[], Awaitable[bool]] | None = None
    refusal: list[Response] = []
    if timestamp is not None:
        # The comparison runs inside propagate_write's path lock, the lock
        # every platform write of the file takes: a write in flight lands
        # before it, and one queued during it lands after this save (never
        # between the check and the write).
        async def _save_check() -> bool:
            nonlocal before
            try:
                before = await asyncio.to_thread(_current_answer, file_id, claims)
            except _Unreadable:
                return True  # not comparable: the save goes on (logged above)
            refused = _conflict(file_id, timestamp, before, request, rel)
            if refused is not None:
                refusal.append(refused)
                return False
            return True
        precheck = _save_check
    await workspace_fanout.propagate_write(
        agent_slug, tree_rel, body, exclude_machine_id=None, writer=_writer,
        precheck=precheck,
    )
    if refusal:
        return refusal[0]
    digest = await asyncio.to_thread(_note_saved_base, file_id, body)
    # Tell OTHER users' dashboards the file changed so an open
    # preview / workspace tree refreshes. source="collabora": a peer with the
    # SAME doc open in Collabora is already live-merged (won't force-reload);
    # a non-Collabora view still refreshes. Exclude the saving person (the
    # token's user_sub: "agent" only for a push of a turn with no person).
    from services.notifications import notification_manager
    await notification_manager.broadcast_file_updated(
        agent_slug, tree_rel, source="collabora",
        exclude_user_sub=claims.get("user_sub", "") or "",
    )
    logger.info(
        "WOPI PutFile: %s (%d bytes) by %s",
        name, len(body), claims.get("user_name"),
    )

    saved = await asyncio.to_thread(_saved_answer, file_id, claims)
    _refresh_version(claims, bare_id, body, digest, saved, before)
    return _put_file_answer(saved)


def _host_cache_write(rel: str, body: bytes) -> bytes | None:
    """Replace the host-cache mirror at ``rel`` beneath ``AGENTS_DIR`` (no
    component followed) and return the bytes it held, None for a new file.
    A link at the name or a mirror over the sync cap refuses the save (an
    ``OSError`` of the helpers). Blocking: a worker-thread call."""
    try:
        prev = safe_fs.read_bytes_beneath(config.AGENTS_DIR, rel, max_size=config.SYNC_MAX_FILE_BYTES)
    except FileNotFoundError:
        prev = None
    safe_fs.atomic_write_beneath(config.AGENTS_DIR, rel, body)
    return prev


@router.post("/wopi/files/{file_id}")
async def wopi_lock_operations(
    request: Request,
    file_id: str,
    access_token: str = Query(...),
):
    """WOPI Lock/Unlock/RefreshLock operations.

    The lock table is keyed by the raw ``file_id`` (one lock per generation
    of a file, like Collabora's documents) and no file is opened: the check
    is the token and the path the id names (an id that does not decode, or
    that names another path, is 403 as on the read routes)."""
    claims = validate_wopi_token(access_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired WOPI token")
    try:
        rel, _generation = decode_wopi_id(file_id)
    except ValueError:
        raise HTTPException(status_code=403, detail="Token/file mismatch")
    if claims.get("file_path") != rel:
        raise HTTPException(status_code=403, detail="Token/file mismatch")

    override = request.headers.get("X-WOPI-Override", "").upper()
    lock_id = request.headers.get("X-WOPI-Lock", "")
    old_lock = request.headers.get("X-WOPI-OldLock", "")

    # Mutating lock ops require edit capability — a view-only session must
    # not be able to place/steal a lock and 409 the real editor's saves.
    # GET_LOCK stays readable with any valid token.
    if override in ("LOCK", "UNLOCK", "REFRESH_LOCK") and claims.get("permissions") != "edit":
        raise HTTPException(status_code=403, detail="Write permission required")

    current = _get_lock(file_id)

    if override == "LOCK":
        if old_lock:
            # Unlock and relock
            if current and current != old_lock:
                return Response(status_code=409, headers={"X-WOPI-Lock": current or ""})
            _set_lock(file_id, lock_id)
        elif current:
            if current == lock_id:
                # RefreshLock
                _set_lock(file_id, lock_id)
            else:
                return Response(status_code=409, headers={"X-WOPI-Lock": current})
        else:
            _set_lock(file_id, lock_id)
        return Response(status_code=200)

    elif override == "UNLOCK":
        if not current or current != lock_id:
            return Response(status_code=409, headers={"X-WOPI-Lock": current or ""})
        _remove_lock(file_id, lock_id)
        return Response(status_code=200)

    elif override == "REFRESH_LOCK":
        if not current or current != lock_id:
            return Response(status_code=409, headers={"X-WOPI-Lock": current or ""})
        _set_lock(file_id, lock_id)
        return Response(status_code=200)

    elif override == "GET_LOCK":
        return Response(status_code=200, headers={"X-WOPI-Lock": current or ""})

    return Response(status_code=501)


# ---------------------------------------------------------------------------
# Dashboard endpoint: generate Collabora WOPI URL
# ---------------------------------------------------------------------------


def build_cool_url(file_id: str) -> str:
    """The Collabora host page URL for a file (its ``WOPISrc`` and the UI
    options), shared by the dashboard endpoints and the document-preview
    hook. The WOPI token is never on it: the dashboard posts
    ``access_token`` and ``access_token_ttl`` into the frame as a form
    (Collabora's host page shape), so no access log, history entry or
    referrer carries the token."""
    wopi_src = urllib.parse.quote(
        f"{config.WOPI_BASE_URL.rstrip('/')}/wopi/files/{file_id}",
        safe="",
    )
    return (
        f"{config.COLLABORA_URL}/browser/dist/cool.html"
        f"?WOPISrc={wopi_src}"
        f"&closebutton=0&homebutton=0"
        f"&ui_defaults=UIMode%3Dcompact%3BTextSidebar%3Dfalse"
        f"%3BSpreadsheetSidebar%3Dfalse%3BPresentationSidebar%3Dfalse"
    )


def cool_frame(file_id: str, token: str, token_ttl: int) -> dict:
    """What the dashboard needs to open the editor: the host page URL and
    the token it posts there."""
    return {"wopi_url": build_cool_url(file_id), "access_token": token,
            "access_token_ttl": token_ttl}


class WopiUrlRequest(BaseModel):
    file_path: str  # relative to agent dir, e.g. "users/alice/workspace/report.docx" or "workspace/report.docx"
    agent: str
    edit: bool = False


@router.post("/v1/documents/wopi-url")
async def generate_wopi_url(
    req: WopiUrlRequest,
    user: UserContext = Depends(get_current_user),
):
    """Generate a Collabora iframe URL for the dashboard."""
    require_auth(user)

    if not config.COLLABORA_URL:
        # Fail loud: an empty COLLABORA_URL would emit a relative iframe src
        # (`/browser/dist/cool.html?...`), which the SPA catch-all happily
        # returns as the dashboard's index.html — the user sees the platform
        # home page inside the iframe with no error to explain it. Raising
        # 503 here makes the misconfig visible to operators.
        raise HTTPException(
            status_code=503,
            detail="Document preview is unavailable: COLLABORA_URL is not configured. Set it in config.env (e.g., https://prifiles.example.com) and restart the proxy.",
        )

    if not user.can_access_agent(req.agent):
        raise HTTPException(status_code=403, detail="No access to agent")

    rel_path, permissions = await run_db(_workspace_mint, req, user)
    file_id = encode_file_id(rel_path)
    token, token_ttl = create_wopi_token(
        rel_path, user.sub, user.name, permissions, req.agent
    )
    # The document of the file's newest push when one was noted, so the
    # workspace tab, the path chips and the chat panes share it (a Collabora
    # save in one is already in the others). ``file_id`` stays the bare id.
    from api.hooks import preview as preview_hook
    frame_id = encode_wopi_id(rel_path, preview_hook.pushed_generation(file_id))
    return {**cool_frame(frame_id, token, token_ttl), "file_id": file_id,
            "permissions": permissions}


def _workspace_mint(req: WopiUrlRequest, user: UserContext) -> tuple[str, str]:
    """``(rel beneath AGENTS_DIR, permissions)`` of a workspace document
    mint: the confinement, the file probe, the read-scope gate and the write
    decision. Filesystem and store reads: run it through ``run_db``; its
    ``HTTPException`` reaches the client unchanged."""
    # Resolve full path. The agent name is a request field too — an admin
    # passes can_access_agent for any string — so confine it to the agents
    # tree before the file path is confined to the agent.
    try:
        agent_root = Path(os.path.realpath(safe_agent_dir(req.agent)))
        full_path = resolve_under(agent_root / req.file_path, agent_root)
    except PathOutsideRoot:
        raise HTTPException(status_code=403, detail="Path must be within agent workspace or users directory")
    agent_rel = full_path.relative_to(agent_root).as_posix()

    # Security: must be within agent workspace or users directory
    if layout.head_of(agent_rel) not in (layout.WORKSPACE, layout.USERS):
        raise HTTPException(status_code=403, detail="Path must be within agent workspace or users directory")

    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Read-scope gate: minting ANY token must be authorized like READING the
    # file — without this a user could mint a (view) token for another user's
    # document. Checked on the RESOLVED agent-relative path. A trusted no-user /
    # service caller (acting_sub None) gets full access, like the file API.
    if user.acting_sub is not None:
        from api.agents.agents import _check_file_role
        from storage import database as _db
        _check_file_role(
            agent_rel, user.acting_role(req.agent), writing=False,
            username=_db.get_username_by_sub(user.sub) or "",
        )

    # Relative path from AGENTS_DIR (for file_id)
    rel_path = str(full_path.relative_to(config.AGENTS_DIR.resolve()))

    # Role-gate the edit decision SERVER-SIDE. The client `req.edit`
    # bool is a request, not authority — without this a viewer could mint an
    # edit token via the API. ``can_write_back`` is the same write matrix the
    # satellite write-back guard + simple file editor enforce; req.file_path is
    # already agent-tree-relative ("workspace/..." or "users/{u}/..."). API-key
    # callers (trusted master key) bypass, mirroring the file API's role check.
    if user.acting_sub is None:
        can_edit = req.edit
    else:
        from core.remote.file_sync import can_write_back, library_mirror_source
        from storage import database as _db
        _uname = _db.get_username_by_sub(user.sub) or ""
        # Authorize the RESOLVED agent-relative path (== req.file_path for a
        # well-formed request) so a '..'-laundered body can't flip the verdict.
        _rel = full_path.relative_to(agent_root).as_posix()
        _wl = None
        if library_mirror_source(_rel) is not None:
            from storage.knowledge import db_knowledge_libraries
            _wl = db_knowledge_libraries.writable_pairs_for(req.agent)
        can_edit = req.edit and can_write_back(
            _rel, user.acting_role(req.agent), _uname,
            writable_libraries=_wl,
        )
    return rel_path, "edit" if can_edit else "view"


@router.get("/v1/documents/snapshot-wopi-url")
async def generate_snapshot_wopi_url(
    chat_id: str = Query(...),
    snapshot_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Mint a VIEW-only Collabora URL for a version-pinned preview snapshot.

    Called by the document pane for each load of a version, never
    persisted. Access is gated on the requester's CHAT access (the snapshot
    belongs to the chat's preview history, not to any workspace path), and
    the snapshot must still be referenced by a non-dismissed preview event.
    404 (pruned/unknown snapshot) returns the pane to the live file with a
    note."""
    u = require_auth(user)

    if not config.COLLABORA_URL:
        raise HTTPException(
            status_code=503,
            detail="Document preview is unavailable: COLLABORA_URL is not configured.",
        )

    chat, event, owner = await run_db(_snapshot_mint, u, chat_id, snapshot_id)
    rel_path = snapshot_rel_path(owner, snapshot_id)
    token, token_ttl = create_wopi_token(
        rel_path, u.sub, u.name, "view", chat.get("agent") or "",
        display_name=event.get("filename") or "document",
    )
    file_id = encode_file_id(rel_path)
    return cool_frame(file_id, token, token_ttl)


def _chat_for_mint(u: UserContext, chat_id: str) -> dict:
    """The chat a chat-gated mint serves, 404 when unknown and 403 when the
    requester may not open it. Store reads: called from the mints' ``run_db``
    helpers."""
    from storage import database as task_store
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    from api.agents.chats import can_access_chat
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")
    return chat


def _page_chat_ids(chat_id: str) -> list[str]:
    """The chats whose rows a chat's page shows, oldest round first: the
    chat itself and, for a task run's chat, the chats of the other runs on
    its session (each round of a multi-turn run has its own ``task-`` chat,
    and the page shows them all, as the resume reads them). The page's own
    chat passed the gate; its rounds ride it. Store reads."""
    from core.session import session_kind
    if not session_kind.is_task_chat_id(chat_id):
        return [chat_id]
    from storage import database as task_store
    run = task_store.get_run(session_kind.run_id_of_chat(chat_id))
    if not run or not run.get("session_id"):
        return [chat_id]
    runs = task_store.list_runs(limit=50, session_id=run["session_id"])
    runs.sort(key=lambda r: r.get("started_at") or "")
    out: list[str] = []
    for r in runs:
        cid = r.get("chat_id")
        if cid and cid not in out:
            out.append(cid)
    if chat_id not in out:
        out.append(chat_id)
    return out


def _snapshot_mint(u: UserContext, chat_id: str, snapshot_id: str) -> tuple[dict, dict, str]:
    """``(chat, preview event, the event's chat id)`` of a snapshot mint: the
    chat gate, the live event that references the snapshot in any chat the
    page shows and the snapshot file's presence under that chat. Store reads
    and a path probe: run it through ``run_db``; its ``HTTPException``
    reaches the client unchanged."""
    chat = _chat_for_mint(u, chat_id)
    from storage import database as task_store
    from services.media import preview_snapshots
    for owner in _page_chat_ids(chat_id):
        event = task_store.get_preview_event_by_snapshot(owner, snapshot_id)
        if event is None:
            continue
        if preview_snapshots.snapshot_path(owner, snapshot_id) is None:
            break
        return chat, event, owner
    raise HTTPException(status_code=404, detail="Snapshot not found")


@router.get("/v1/documents/preview-wopi-url")
async def generate_preview_wopi_url(
    chat_id: str = Query(...),
    file_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """Mint a token for the live file in a chat's document pane.

    A stored preview row carries no token, so the pane calls this for each
    load of the live file unless a push of it under 15 minutes old is in
    memory; the returned URL is never persisted. Access
    mirrors snapshot-wopi-url: the requester's CHAT access plus a live
    (non-dismissed) preview event for this file_id. Write capability is
    recomputed for the REQUESTER — same rules as the push-time mint: workspace
    files go through the agent-tree write matrix; a host-cache mirror (a file
    on the remote machine's own disk) is editable by write-capable roles only
    within the chat's own session. 404 tells the pane to use a pushed token
    when it has one, else that the file is gone."""
    u = require_auth(user)

    if not config.COLLABORA_URL:
        raise HTTPException(
            status_code=503,
            detail="Document preview is unavailable: COLLABORA_URL is not configured.",
        )

    rel_path, permissions, agent, owner, row_generation = await run_db(
        _preview_mint, u, chat_id, file_id)
    from api.hooks import preview as preview_hook
    generation = preview_hook.mint_generation(file_id, row_generation)
    token, token_ttl = create_wopi_token(rel_path, u.sub, u.name, permissions, agent,
                                         chat_id=owner)
    # The frame opens the document of the file's newest push: the token is
    # the bare path's, which every generation of the id answers to.
    return {**cool_frame(encode_wopi_id(rel_path, generation), token, token_ttl),
            "permissions": permissions}


def _preview_mint(u: UserContext, chat_id: str, file_id: str) -> tuple[str, str, str, str, int]:
    """``(rel beneath AGENTS_DIR, permissions, agent claim, chat claim, the
    newest push generation)`` of a live preview mint: the chat gate, the
    live event in a chat the page shows (the chat claim is the newest round
    holding one, where a save refreshes the file's newest version), the
    path confinement and file probe, and the requester's write decision.
    The generation is the document generation the newest row's push opened
    (its stored URL's WOPISrc, else its push time): the route prefers the
    one the hook noted (``preview.mint_generation``, on the loop). Store
    reads and path probes: run it through ``run_db``; its ``HTTPException``
    reaches the client unchanged."""
    chat = _chat_for_mint(u, chat_id)
    from storage import database as task_store
    owner, event = None, None
    for c in reversed(_page_chat_ids(chat_id)):
        event = task_store.get_preview_event_by_file(c, file_id)
        if event is not None:
            owner = c
            break
    if owner is None or event is None:
        raise HTTPException(status_code=404, detail="Preview not found")
    generation = _row_document_generation(event)

    try:
        rel_path = decode_file_id(file_id)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=404, detail="Preview not found")
    # Preview events only ever reference agent-tree files; the snapshot
    # namespace has its own endpoint (with snapshot-specific display naming).
    if rel_path.startswith(_SNAPSHOT_NS + "/"):
        raise HTTPException(status_code=404, detail="Preview not found")
    full_path = (config.AGENTS_DIR / rel_path).resolve()
    if not full_path.is_relative_to(config.AGENTS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="Preview not found")

    agent = chat.get("agent") or ""
    # A meeting participant's preview sits in the participant's tree: the
    # claim names the tree the path is in (the role is still the requester's
    # on that agent), so the token says what it covers.
    _first = rel_path.partition("/")[0]
    if _first != agent and _first != HOST_CACHE_SEGMENT and config.is_safe_agent_name(_first):
        agent = _first
    from storage import database as _db
    _session_id = chat.get("session_id") or ""
    _is_host_cache = False
    if _session_id:
        from core.remote import remote_file_flow
        with contextlib.suppress(OSError):
            _is_host_cache = full_path.is_relative_to(
                remote_file_flow.host_cache_session_root(_session_id).resolve()
            )
    permissions = live_permissions(
        rel_path.partition("/")[2], u.acting_role(agent), _db.get_username_by_sub(u.sub) or "",
        agent, host_cache=_is_host_cache,
    )
    return rel_path, permissions, agent, owner, generation


def _row_document_generation(event: dict) -> int:
    """The document generation a stored push opened: the one its URL's
    WOPISrc carries, else its push time (a row from before 1.7.1)."""
    try:
        src = urllib.parse.parse_qs(urllib.parse.urlparse(event.get("wopi_url") or "").query)
        return decode_wopi_id(src["WOPISrc"][0].rsplit("/", 1)[-1])[1] or int(event.get("generation") or 0)
    except (KeyError, IndexError, ValueError):
        return int(event.get("generation") or 0)


def live_permissions(tree_rel: str, role: str, username: str, agent: str, *,
                     host_cache: bool) -> str:
    """``"edit"`` or ``"view"``: what one person's token for a chat's live
    document allows (the pane's mint and the push's token for the person
    the turn runs as). A host-cache mirror (a file on the session's own
    machine) is editable by a write-capable role, PutFile pushing the bytes
    back to the real file, where the satellite's own host write policy is
    the authoritative gate. An agent-tree file follows the agent-tree write
    matrix. Store reads: call it on the DB executor."""
    if host_cache:
        return "edit" if roles.can_write_workspace(role) else "view"
    from core.remote.file_sync import can_write_back, library_mirror_source
    writable = None
    if library_mirror_source(tree_rel) is not None:
        from storage.knowledge import db_knowledge_libraries
        writable = db_knowledge_libraries.writable_pairs_for(agent)
    return "edit" if can_write_back(tree_rel, role, username, writable_libraries=writable) else "view"


@router.get("/v1/documents/chat-documents")
async def list_chat_documents(
    chat_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
):
    """The documents pushed into a chat, for its document pane: each file
    with its newest push's name and download link, and its versions newest
    first (``version`` = the push's 1-based position among the file's
    pushes, ``turn`` = the person's messages up to it, ``generation`` = the
    push time in ms, ``available`` = the version's copy still exists).
    Gated like the mints: the requester's chat access. No WOPI URL and no
    token: the pane mints those per file and per version."""
    u = require_auth(user)
    return {"documents": await run_db(_chat_documents, u, chat_id)}


def _chat_documents(u: UserContext, chat_id: str) -> list[dict]:
    """The listing of ``list_chat_documents``. Store reads and snapshot
    stats: run it through ``run_db``; its ``HTTPException`` reaches the
    client unchanged."""
    _chat_for_mint(u, chat_id)
    from storage import database as task_store
    from services.media import preview_snapshots
    by_file: dict[str, dict] = {}
    for row in task_store.get_chat_preview_listing_rows(_page_chat_ids(chat_id)):
        data = row["data"]
        file_id = data.get("file_id") or ""
        if not file_id:
            continue
        doc = by_file.setdefault(file_id, {"file_id": file_id, "versions": []})
        doc["filename"] = data.get("filename") or ""
        doc["download_url"] = data.get("download_url") or ""
        doc["generation"] = row["generation"]
        snapshot_id = data.get("snapshot_id") or ""
        doc["versions"].append({
            "snapshot_id": snapshot_id,
            "message_id": row["id"],
            "version": len(doc["versions"]) + 1,
            "generation": row["generation"],
            "turn": row["turn"],
            "available": bool(snapshot_id)
            and preview_snapshots.snapshot_path(row["chat_id"], snapshot_id) is not None,
        })
    for doc in by_file.values():
        doc["versions"].reverse()
    return sorted(by_file.values(), key=lambda d: d["generation"], reverse=True)
