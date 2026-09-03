"""File Upload REST API.

Provides a multipart upload endpoint for files of any type, plus the chunked
sibling endpoints (init → PUT chunks → complete) that let browsers move files
of any size through CDN/gateway request-body caps. Files are saved to the
agent's per-user workspace directory.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi import File as FileParam
from pydantic import BaseModel

import config
from storage import agent_store
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from storage import database as task_store

logger = logging.getLogger("claude-proxy.uploads")
router = APIRouter()

# The per-file cap is the UNIVERSAL config.MAX_UPLOAD_SIZE_BYTES
# (OTODOCK_MAX_FILE_MB, default 1GB) — read live in the endpoint so tests and
# per-install overrides apply without an import-order dance. The old
# generic-vs-media split collapsed when the universal cap landed.

# Audio/video extensions (still used for labels/routing decisions elsewhere).
# Kept in sync with the frontend AUDIO_EXTENSIONS/VIDEO_EXTENSIONS in
# dashboard/src/lib/fileTypes.ts.
MEDIA_EXTENSIONS = {
    # audio
    ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac",
    # video
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi",
}

# There is deliberately NO extension allowlist: agents work in full dev
# environments, so any file a user has is a legitimate upload (.psd, .gcode,
# .dwg, source trees, extensionless Makefiles…). Safe because uploads are
# inert data here — the platform never executes them, agents can already
# write arbitrary bytes into the same workspace themselves, and every serving
# route forces non-inert types (html/svg/xml/…) to ``Content-Disposition:
# attachment`` + ``nosniff`` so nothing uploaded can render as a same-origin
# document (see ``api/agents/files.py`` / ``api/media/media.py`` — any NEW
# raw-serving route must keep that inline-allowlist rule).

FILE_TYPE_LABELS = {
    ".pdf": "PDF document",
    ".docx": "Word document",
    ".xlsx": "Excel spreadsheet",
    ".pptx": "PowerPoint presentation",
    ".csv": "CSV data",
    ".json": "JSON data",
    ".txt": "Text file",
    ".md": "Markdown document",
    ".xml": "XML document",
    ".yaml": "YAML configuration",
    ".yml": "YAML configuration",
    ".html": "HTML document",
    ".zip": "ZIP archive",
    ".mp3": "MP3 audio",
    ".m4a": "M4A audio",
    ".aac": "AAC audio",
    ".wav": "WAV audio",
    ".ogg": "OGG audio",
    ".oga": "OGG audio",
    ".opus": "Opus audio",
    ".flac": "FLAC audio",
    ".mp4": "MP4 video",
    ".m4v": "MP4 video",
    ".mov": "QuickTime video",
    ".webm": "WebM video",
    ".mkv": "Matroska video",
    ".avi": "AVI video",
    ".jpg": "JPEG image",
    ".jpeg": "JPEG image",
    ".png": "PNG image",
    ".gif": "GIF image",
    ".webp": "WebP image",
    ".bmp": "BMP image",
    ".tiff": "TIFF image",
    ".tif": "TIFF image",
    ".svg": "SVG image",
}


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename for safe filesystem storage."""
    # Strip path separators
    name = name.replace("/", "_").replace("\\", "_")
    # Replace unsafe chars (keep alphanumeric, dot, hyphen, underscore, space)
    name = re.sub(r"[^\w.\- ]", "_", name)
    # Collapse multiple underscores/spaces
    name = re.sub(r"[_ ]{2,}", "_", name).strip("_ ")
    # Limit length (preserve extension)
    stem = Path(name).stem[:180]
    ext = Path(name).suffix
    return f"{stem}{ext}" if stem else f"file{ext}"


def _resolve_conflict(target: Path) -> Path:
    """Append _1, _2, etc. if the target file already exists."""
    if not target.exists():
        return target
    stem = target.stem
    ext = target.suffix
    parent = target.parent
    for i in range(1, 100):
        candidate = parent / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail="Too many files with this name")


def _resolve_upload_destination(
    user: UserContext, agent: str, target_dir: str, safe_name: str,
    *, create: bool,
) -> tuple[Path, Path]:
    """Authorize + resolve the landing directory for an upload.

    The FULL chain the single-shot route has always run — agent existence,
    agent-scoped vs per-user landing, the manager gate for agent-scoped
    targets, role-checked `safe_agent_path` for explicit target dirs, and the
    resolved-path confinement check — shared by the single-shot route and the
    chunked init/complete endpoints so the two paths can never drift. Chunked
    callers run it TWICE by design: at init with ``create=False`` (fail before
    any bytes move, but leave no empty dir behind an aborted upload) and at
    complete with ``create=True`` (the caller's role may have changed since
    init, and the dir must exist for the rename).

    Returns ``(upload_dir, agent_dir)``.
    """
    if not agent_store.agent_exists(agent):
        raise HTTPException(status_code=400, detail=f"Unknown agent: {agent}")

    # Shared-only agents (incl. service agents like the phone caller) mount the
    # agent scope even for human chats, so uploads go in the shared agent
    # workspace, not a per-user dir. See core/session/visibility.py.
    from core.session.visibility import is_shared_only
    is_agent_scoped = is_shared_only(agent)

    # Resolve username (only required for user-scoped uploads — agent-scoped
    # writes go under `<agent_dir>/workspace/`).
    username = task_store.get_username_by_sub(user.sub) or ""
    if not is_agent_scoped and not username:
        raise HTTPException(status_code=400, detail="User has no username configured")

    agent_dir = config.get_agent_dir(agent)
    if target_dir:
        # Custom target — authorize the RESOLVED final path against the caller's
        # per-agent role (fixes a role-var bug — was user.role — and defeats
        # '..' / symlink scope-escape).
        from api.agents.agents import safe_agent_path
        target_file, _ = safe_agent_path(
            agent_dir, agent, str(Path(target_dir)) + "/" + safe_name, user, writing=True,
        )
        upload_dir = target_file.parent
    elif is_agent_scoped:
        # Agent-scoped chat upload — manager+ writes to the shared workspace.
        # Path policy (`auth/path_policy._check_write_path`) confirms
        # `/workspace/` is writable for agent-scoped sessions, but the API
        # caller is a real user — gate on per-agent manager role to keep
        # viewers from posting into shared workspace via internal-agent chats.
        if not user.can_manage_agent(agent):
            raise HTTPException(
                status_code=403,
                detail="Manager role required to upload to agent workspace",
            )
        upload_dir = agent_dir / "workspace" / "uploads" / "files"
    else:
        # Default chat-upload destination — dedicated subfolder under the
        # user's workspace to keep the root tidy. Workspace-page uploads
        # pass an explicit `target_dir` and bypass this default. Mirrors
        # the workspace-tidiness pattern used by image-gen-mcp
        # (`generated-assets/`) and the WS chat-photo path
        # (`uploads/photos/`).
        upload_dir = (
            agent_dir / "users" / username / "workspace" / "uploads" / "files"
        )

    if create:
        upload_dir.mkdir(parents=True, exist_ok=True)

    # Security: ensure resolved path is within agent dir
    resolved_dir = upload_dir.resolve()
    agent_root = agent_dir.resolve()
    if resolved_dir != agent_root and not resolved_dir.is_relative_to(agent_root):
        raise HTTPException(status_code=403, detail="Path outside agent directory")

    return upload_dir, agent_dir


@router.post("/v1/upload")
async def upload_file(
    request: Request,
    file: UploadFile = FileParam(...),
    agent: str = Form(...),
    target_dir: str = Form(""),
    user: UserContext | None = Depends(get_current_user),
):
    """Upload a binary file to an agent directory.

    Args:
        file: The file to upload (multipart).
        agent: Agent name.
        target_dir: Optional relative path within agent dir (e.g. "config/context").
                    If empty, defaults to users/{username}/workspace/.
                    Validated via role-based _check_file_role.
    """
    user = require_auth(user)
    require_agent_access(user, agent)

    original_name = file.filename or "unnamed"
    ext = Path(original_name).suffix.lower()

    # Universal per-file cap (OTODOCK_MAX_FILE_MB) — same for every file type.
    size_cap = config.MAX_UPLOAD_SIZE_BYTES
    cap_mb = size_cap // (1024 * 1024)

    # Sanitize filename
    safe_name = _sanitize_filename(original_name)
    if not safe_name:
        safe_name = f"file{ext}"

    upload_dir, agent_dir = _resolve_upload_destination(
        user, agent, target_dir, safe_name, create=False,
    )

    # Quick size check via Content-Length header
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > size_cap + 1024:  # small margin for form fields
        raise HTTPException(status_code=413, detail=f"File too large (max {cap_mb} MB)")

    upload_dir.mkdir(parents=True, exist_ok=True)
    target = _resolve_conflict(upload_dir / safe_name)

    # Stream to a `.partial` sibling, then atomic-rename into place: a big
    # upload has a long failure window, and a torn final file must never be
    # visible. `.partial` is symmetrically excluded from sync manifests and
    # fan-out, so a half-received upload is sync-invisible by construction.
    tmp = target.with_name(target.name + ".partial")
    total_bytes = 0
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > size_cap:
                    raise HTTPException(
                        status_code=413, detail=f"File too large (max {cap_mb} MB)"
                    )
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")

    rel_path = str(target.relative_to(agent_dir))
    logger.info(f"File uploaded: {rel_path} ({total_bytes} bytes) by user={user.sub[:16]}...")

    # If any active session for this agent runs on a remote satellite, push
    # the upload over so the agent CLI can see it — in the BACKGROUND, so
    # the response (and the dashboard's workspace listing, which reads the
    # platform dir) doesn't stall for the length of a WAN transfer. A prompt
    # referencing the file can't outrun the push: the remote turn dispatch
    # barriers on in-flight pushes (``core/remote/upload_inflight``).
    # Best-effort like the old synchronous push — on failure the periodic
    # fingerprint sweep / next session-start sync reconciles.
    #
    # transfer_id is minted HERE so the response and the phase-2 progress
    # events (transfer registry, Feature E) share one id — the uploading tab
    # links its local phase-1 item to the server-side machine rows by it.
    import uuid as _uuid
    transfer_id = str(_uuid.uuid4())
    pushed = _schedule_upload_push(
        agent, rel_path, target,
        transfer_id=transfer_id, origin_user_sub=user.sub,
    )

    return {
        "path": rel_path,
        "filename": target.name,
        "size": total_bytes,
        "transfer_id": transfer_id,
        # False → no connected machine will receive this upload; the client
        # completes its progress item at upload end instead of waiting for
        # phase-2 events that will never come.
        "remote_push": pushed,
    }


def _schedule_upload_push(
    agent_slug: str, rel_path: str, host_path: Path, *,
    transfer_id: str | None = None, origin_user_sub: str = "",
) -> bool:
    """Background ``_push_upload_to_active_remote_sessions`` for a fresh
    upload, registered with the turn-start barrier
    (``core/remote/upload_inflight``). Never raises, never blocks. Returns
    True iff a push was actually scheduled (fan-out candidates exist).

    The cheap in-memory candidate gate runs HERE (synchronously) so
    local-only installs — no connected satellite — schedule nothing at all.
    """
    try:
        from services.remote import workspace_fanout
        if not workspace_fanout.has_fanout_candidates(
            agent_slug, rel_path, include_idle=True,
        ):
            return False
        from core.remote import upload_inflight

        async def _push() -> None:
            try:
                await _push_upload_to_active_remote_sessions(
                    agent_slug, rel_path, host_path,
                    transfer_id=transfer_id, origin_user_sub=origin_user_sub,
                )
            except Exception:
                logger.exception(
                    "Failed to push upload to remote sessions: %s", rel_path,
                )

        upload_inflight.track(agent_slug, _push())
        return True
    except Exception:
        logger.exception("Failed to schedule upload push: %s", rel_path)
        return False


async def _push_upload_to_active_remote_sessions(
    agent_slug: str, rel_path: str, host_path: "Path", *,
    transfer_id: str | None = None, origin_user_sub: str = "",
) -> None:
    """Push a freshly-uploaded file to active remote sessions of this agent, via
    the isolation-aware fan-out.

    Routes through ``services/remote/workspace_fanout`` so per-user / per-role isolation
    applies: an upload under ``users/{alice}/`` only reaches machines whose active
    session may actually see it — not every machine running the agent (fixes the
    historical "push to every machine" leak). No-op when no allowed remote
    session is active.

    Callers split by how they wait:
      * ``/v1/upload`` runs this in the BACKGROUND via ``_schedule_upload_push``
        (the remote turn dispatch barriers on it — ``core/remote/upload_inflight``);
      * the ws/dashboard "Take Photo / Upload Photo" path and the hook-side
        artifact writes (``api/hooks/hooks.py``) AWAIT it — they run inside a
        message send / agent turn where the file must be on the machine before
        the very next step reads it.
    """
    from services.remote import workspace_fanout
    if not workspace_fanout.has_fanout_candidates(agent_slug, rel_path, include_idle=True):
        return
    # Pass the PATH — push_file streams from disk, so a 1GB upload fanning out
    # to N machines never holds the file in memory.
    await workspace_fanout.fan_out_write(
        agent_slug, rel_path, host_path, include_idle=True,
        transfer_kind="upload", transfer_id=transfer_id,
        origin_user_sub=origin_user_sub,
    )


# ---------------------------------------------------------------------------
# Chunked uploads (edge-cap-proof): init → PUT chunks → complete
#
# Browsers slice files bigger than one chunk into N sequential raw-body PUTs,
# so a CDN/gateway request-body cap (Cloudflare ~100MB, nginx
# client_max_body_size) never sees the whole file, and a network blip retries
# one chunk instead of restarting a 300MB upload. Staging lives in
# config.UPLOAD_STAGING_DIR — a proxy-private SIBLING of the agents dir. On
# bare metal that is the same filesystem (the final os.replace is a pure
# rename); in Docker the agents dir is a NAMED VOLUME and the staging dir
# sits in the container overlay, so `_finalize_staged_file` falls back to a
# same-directory temp copy + atomic rename on EXDEV (every chunked finalize
# 500'd there until 2026-09-02). Either way staging sits
# outside every agent tree, so in-flight staging is invisible to sync
# manifests, agent shells, and the `*.partial` retention reaper that patrols
# AGENTS_DIR. State is disk-first (a meta json beside the staging file):
# status/resume survive a proxy restart with no in-memory registry to
# invalidate.
# ---------------------------------------------------------------------------

# Staging pairs older than this are swept lazily on init. Meta mtime bumps on
# every received chunk, so only genuinely abandoned uploads age out.
_STAGING_TTL_S = 24 * 3600

# Serializes the staging write + meta update per upload id. The shipped
# client PUTs strictly sequentially; the lock is insurance against a
# misbehaving caller racing two PUTs into a read-modify-write meta loss.
_chunk_locks: dict[str, asyncio.Lock] = {}


class ChunkedInitRequest(BaseModel):
    agent: str
    filename: str
    size: int
    target_dir: str = ""


# token_urlsafe(16) → 22 chars of [A-Za-z0-9_-]; anything else in the path
# param is hostile (upload_id feeds filesystem paths — this regex is the
# traversal guard, not just tidiness).
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _staging_paths(upload_id: str) -> tuple[Path, Path]:
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=404, detail="Unknown upload id")
    d = config.UPLOAD_STAGING_DIR
    return d / f"{upload_id}.partial", d / f"{upload_id}.json"


def _load_chunk_meta(upload_id: str, user: UserContext) -> tuple[dict, Path, Path]:
    """Meta for an in-flight chunked upload, owner- and access-checked.

    404 for an unknown id (or a swept meta); 403 when the caller isn't the
    user who ran init. Re-runs the agent-access check — access may have been
    revoked mid-upload.
    """
    staging, meta_path = _staging_paths(upload_id)
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Unknown upload id")
    if meta.get("sub") != user.sub:
        raise HTTPException(status_code=403, detail="Not your upload")
    require_agent_access(user, meta.get("agent", ""))
    return meta, staging, meta_path


def _save_chunk_meta_atomic(meta_path: Path, meta: dict) -> None:
    # Temp + os.replace: a crash mid-write must never corrupt the resume state.
    tmp = meta_path.with_name(meta_path.name + ".tmp")
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, meta_path)


def _sweep_stale_staging() -> None:
    """Unlink staging/meta files idle beyond the TTL (lazy, called from init)."""
    try:
        d = config.UPLOAD_STAGING_DIR
        if not d.is_dir():
            return
        cutoff = time.time() - _STAGING_TTL_S
        for p in d.iterdir():
            try:
                if p.suffix in (".partial", ".json", ".tmp") and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                continue
    except Exception:
        logger.exception("stale chunked-upload staging sweep failed")


@router.post("/v1/upload/chunked/init")
async def chunked_upload_init(
    body: ChunkedInitRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Open a chunked upload: validate cap + destination, create staging.

    The server DICTATES the chunk size (clients slice strictly by the
    returned value — a negotiated size would invite silent offset
    corruption). The destination is validated NOW so an unauthorized or
    over-cap upload fails in one tiny round-trip, but the landing dir is not
    created yet — an aborted upload must not leave an empty dir behind.
    """
    user = require_auth(user)
    require_agent_access(user, body.agent)

    size_cap = config.MAX_UPLOAD_SIZE_BYTES
    cap_mb = size_cap // (1024 * 1024)
    if body.size <= 0:
        raise HTTPException(status_code=400, detail="Invalid file size")
    if body.size > size_cap:
        raise HTTPException(status_code=413, detail=f"File too large (max {cap_mb} MB)")

    safe_name = _sanitize_filename(body.filename or "unnamed")
    if not safe_name:
        safe_name = "file"
    _resolve_upload_destination(
        user, body.agent, body.target_dir, safe_name, create=False,
    )

    _sweep_stale_staging()

    upload_id = secrets.token_urlsafe(16)
    chunk_size = config.UPLOAD_CHUNK_BYTES
    staging, meta_path = _staging_paths(upload_id)
    config.UPLOAD_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging.touch()
    _save_chunk_meta_atomic(meta_path, {
        "sub": user.sub,
        "agent": body.agent,
        "target_dir": body.target_dir,
        "filename": safe_name,
        "size": body.size,
        "chunk_size": chunk_size,
        "received": [],
    })
    return {"upload_id": upload_id, "chunk_size": chunk_size}


@router.get("/v1/upload/chunked/{upload_id}")
async def chunked_upload_status(
    upload_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Received-chunk indexes — what makes a blip a resume, not a restart."""
    user = require_auth(user)
    meta, _staging, _meta_path = _load_chunk_meta(upload_id, user)
    return {
        "received": sorted(meta.get("received", [])),
        "chunk_size": meta["chunk_size"],
        "size": meta["size"],
    }


@router.put("/v1/upload/chunked/{upload_id}/{index}")
async def chunked_upload_chunk(
    upload_id: str,
    index: int,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Receive one raw-body chunk at its offset. Idempotent per index.

    The body is read via ``request.stream()`` with an in-loop cumulative cap:
    the global body-size middleware is Content-Length-only, so a
    Transfer-Encoding: chunked request would slip past it unmetered. Every
    index must arrive at EXACTLY its expected size — offsets are
    ``index * chunk_size`` and a short/long chunk would corrupt the assembly
    silently. Per-chunk fsync is deliberately stronger than the single-shot
    route's one-fsync-per-file: a 2xx here is a durability promise the
    client's resume logic relies on.
    """
    user = require_auth(user)
    meta, staging, meta_path = _load_chunk_meta(upload_id, user)
    if not staging.exists():
        # Meta survived but staging was swept (TTL) — unrecoverable.
        meta_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=410, detail="Upload staging expired — restart the upload",
        )

    size = int(meta["size"])
    chunk_size = int(meta["chunk_size"])
    n_chunks = (size + chunk_size - 1) // chunk_size
    if index < 0 or index >= n_chunks:
        raise HTTPException(status_code=400, detail="Chunk index out of range")
    expected = min(chunk_size, size - index * chunk_size)

    lock = _chunk_locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        received = 0
        try:
            with open(staging, "r+b") as f:
                f.seek(index * chunk_size)
                async for part in request.stream():
                    if not part:
                        continue
                    received += len(part)
                    if received > expected:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Chunk {index} larger than expected ({expected} bytes)",
                        )
                    f.write(part)
                f.flush()
                os.fsync(f.fileno())
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Chunk write failed ({upload_id}/{index}): {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Chunk write failed")
        if received != expected:
            raise HTTPException(
                status_code=400,
                detail=f"Chunk {index} size mismatch (got {received}, expected {expected})",
            )
        if index not in meta["received"]:
            meta["received"] = sorted([*meta["received"], index])
            _save_chunk_meta_atomic(meta_path, meta)
    return {"received": len(meta["received"]), "total": n_chunks}


def _finalize_staged_file(staging: Path, target: Path, upload_id: str) -> None:
    """Move the assembled staging file into place, atomically.

    Fast path: ``os.replace`` (staging is a sibling of the agents dir on a
    bare-metal install). In Docker the agents dir is a NAMED VOLUME while the
    staging dir lives in the container overlay — ``os.replace`` fails with
    EXDEV (found live 2026-09-02: every chunked upload finalize 500'd). Then:
    copy into a temp file INSIDE the target directory and ``os.replace``
    that, so the destination is still never observable half-written.
    """
    import errno
    import shutil

    try:
        os.replace(staging, target)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    tmp = target.with_name(f".{target.name}.{upload_id}.part")
    try:
        shutil.copyfile(staging, tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    staging.unlink(missing_ok=True)


@router.post("/v1/upload/chunked/{upload_id}/complete")
async def chunked_upload_complete(
    upload_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Assemble-finish: verify, authorize AGAIN, atomic-rename into place.

    Re-runs the full destination chain with ``create=True`` — the caller's
    role may have changed since init, and the conflict-resolved final name
    must be picked at the moment the file actually lands. Returns the exact
    same shape as ``POST /v1/upload`` so callers can't tell the paths apart.
    """
    user = require_auth(user)
    meta, staging, meta_path = _load_chunk_meta(upload_id, user)
    if not staging.exists():
        meta_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=410, detail="Upload staging expired — restart the upload",
        )

    size = int(meta["size"])
    chunk_size = int(meta["chunk_size"])
    n_chunks = (size + chunk_size - 1) // chunk_size
    if len(meta.get("received", [])) != n_chunks:
        missing = n_chunks - len(meta.get("received", []))
        raise HTTPException(
            status_code=409, detail=f"Upload incomplete ({missing} chunks missing)",
        )
    actual = staging.stat().st_size
    if actual != size:
        # Should be unreachable given the exact per-chunk size checks; a
        # mismatch means torn staging — drop it so the client restarts clean.
        staging.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        _chunk_locks.pop(upload_id, None)
        raise HTTPException(
            status_code=409, detail="Assembled size mismatch — restart the upload",
        )
    # The cap may have been LOWERED between init and complete — re-check.
    size_cap = config.MAX_UPLOAD_SIZE_BYTES
    if size > size_cap:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {size_cap // (1024 * 1024)} MB)",
        )

    upload_dir, agent_dir = _resolve_upload_destination(
        user, meta["agent"], meta.get("target_dir", ""), meta["filename"],
        create=True,
    )
    target = _resolve_conflict(upload_dir / meta["filename"])
    try:
        _finalize_staged_file(staging, target, upload_id)
    except OSError as e:
        logger.error(f"Chunked upload finalize failed ({upload_id}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")
    meta_path.unlink(missing_ok=True)
    _chunk_locks.pop(upload_id, None)

    rel_path = str(target.relative_to(agent_dir))
    logger.info(
        f"File uploaded (chunked, {n_chunks} chunks): {rel_path} "
        f"({actual} bytes) by user={user.sub[:16]}..."
    )

    import uuid as _uuid
    transfer_id = str(_uuid.uuid4())
    pushed = _schedule_upload_push(
        meta["agent"], rel_path, target,
        transfer_id=transfer_id, origin_user_sub=user.sub,
    )
    return {
        "path": rel_path,
        "filename": target.name,
        "size": actual,
        "transfer_id": transfer_id,
        "remote_push": pushed,
    }


@router.delete("/v1/upload/chunked/{upload_id}")
async def chunked_upload_abort(
    upload_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Abort cleanup (idempotent — a repeated DELETE after a sweep is fine)."""
    user = require_auth(user)
    staging, meta_path = _staging_paths(upload_id)
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {"ok": True}  # already gone
    if meta.get("sub") != user.sub:
        raise HTTPException(status_code=403, detail="Not your upload")
    staging.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    _chunk_locks.pop(upload_id, None)
    return {"ok": True}
