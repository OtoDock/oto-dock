"""File Upload REST API.

Provides a multipart upload endpoint for files of any type, plus the chunked
sibling endpoints (init → PUT chunks → complete) that let browsers move files
of any size through CDN/gateway request-body caps. Files are saved to the
agent's per-user workspace directory.

Every landing write goes through ``safe_fs`` rooted at the agents tree (a
planted link at the landing name is never written through: the final name
is reserved by a no-replace rename), and every disk commit (the writes, the
fsync, the rename or the cross-device copy) runs on the file-commit executor
(``core/file_commit``), never on the event loop. Staging is per user and
capped (open uploads, staged bytes), every write is refused below a
free-disk floor, and stale staging is reaped off the request path.
"""

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi import File as FileParam
from pydantic import BaseModel

import config
from storage.agents import agent_store
from auth import rate_limiter
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from core import file_commit, layout
from services.infra import safe_fs
from services.infra.path_confinement import (
    PathOutsideRoot, join_under, resolve_under, safe_agent_dir,
)
from storage import database as task_store
from storage.pg import run_db

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

_TEMP_STEM_BYTES = 120
# Request-body pieces are gathered into batches of about this size before a
# write; write(2) blocks under dirty-page throttling on a slow disk, so no
# write runs on the loop.
_WRITE_BATCH_BYTES = 1024 * 1024
# Free-name candidates tried for one landing name (``name``, ``name_1``, …).
_MAX_NAME_TRIES = 100


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


def _resolve_upload_destination(
    user: UserContext, agent: str, target_dir: str, safe_name: str,
    *, create: bool,
) -> tuple[Path, Path]:
    """Authorize + resolve the landing directory for an upload.

    The FULL chain the single-shot route has always run — agent existence,
    agent-scoped vs per-user landing, the workspace-tier gate for agent-scoped
    targets, role-checked `safe_agent_path` for explicit target dirs, and the
    resolved-path confinement check — shared by the single-shot route and the
    chunked init/complete endpoints so the two paths can never drift. Chunked
    callers run it TWICE by design: at init (fail before any bytes move) and
    at complete (the caller's role may have changed since init). It reads the
    DB, so callers run it on ``run_db``; the landing dir itself is created by
    the file-commit thread through ``safe_fs`` (``create`` keeps the plain
    mkdir for the callers that still want it).

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

    agent_dir = safe_agent_dir(agent)
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
        # Agent-scoped chat upload — a shared-workspace write, so the
        # workspace tier (contributor and up). Path policy
        # (`auth/path_policy._check_write_path`) confirms `/workspace/` is
        # writable for agent-scoped sessions, but the API caller is a real
        # user — gate on their per-agent role to keep viewers from posting
        # into the shared workspace via Shared-only chats.
        if not user.can_write_workspace(agent):
            raise HTTPException(
                status_code=403,
                detail="Contributor role or above is required to upload "
                       "to the shared workspace",
            )
        upload_dir = agent_dir / layout.WORKSPACE / "uploads" / "files"
    else:
        # Default chat-upload destination — dedicated subfolder under the
        # user's workspace to keep the root tidy. Workspace-page uploads
        # pass an explicit `target_dir` and bypass this default. Mirrors
        # the workspace-tidiness pattern used by image-gen-mcp
        # (`generated-assets/`) and the WS chat-photo path
        # (`uploads/photos/`).
        upload_dir = (
            layout.user_dir(agent_dir, username) / layout.WORKSPACE / "uploads" / "files"
        )

    # Every branch lands inside the agent tree by construction; re-check it
    # on the RESOLVED landing dir so a symlink planted there cannot redirect
    # the write, and hand back resolved paths so ``relative_to`` agrees.
    agent_root = Path(os.path.realpath(agent_dir))
    try:
        upload_dir = resolve_under(upload_dir, agent_root)
    except PathOutsideRoot:
        raise HTTPException(status_code=403, detail="Path outside agent directory")
    if create:
        upload_dir.mkdir(parents=True, exist_ok=True)

    return upload_dir, agent_root


# ---------------------------------------------------------------------------
# The landing write, shared by both routes: safe_fs under AGENTS_DIR, on the
# file-commit executor
# ---------------------------------------------------------------------------


def _landing_rel(upload_dir: Path) -> str:
    """The landing dir as a rel beneath ``AGENTS_DIR``; a ``SafeFsError`` is
    the caller's 403 (the resolved dir left the agents tree)."""
    return safe_fs.rel_under(upload_dir, config.AGENTS_DIR)


def _rel_to_agent_path(landed_rel: str) -> str:
    """``<agent>/users/...`` → ``users/...`` (the path the API answers)."""
    return landed_rel.split("/", 1)[1] if "/" in landed_rel else ""


def _free_disk_ok(path: Path, incoming: int) -> bool:
    """Whether ``incoming`` more bytes under ``path`` leave the filesystem
    above the floor (the larger of ``MIN_FREE_DISK_MB`` and
    ``MIN_FREE_DISK_PCT``; both 0 = no floor). One statvfs: run it where the
    caller already blocks (a thread), never on the loop, and on a directory
    that exists."""
    floor_mb = config.MIN_FREE_DISK_MB
    floor_pct = config.MIN_FREE_DISK_PCT
    if floor_mb <= 0 and floor_pct <= 0:
        return True
    usage = shutil.disk_usage(path)
    floor = max(floor_mb * 1024 * 1024, int(usage.total * floor_pct / 100.0))
    return usage.free - incoming >= floor


def _insufficient_storage() -> HTTPException:
    return HTTPException(
        status_code=507,
        detail="Not enough free disk space on the platform for this upload",
    )


def _write_batch(f, data: bytes) -> None:
    f.write(data)


def _flush_fsync_close(f) -> None:
    try:
        f.flush()
        os.fsync(f.fileno())
    finally:
        f.close()


def _free_name_candidates(name: str):
    stem, ext = Path(name).stem, Path(name).suffix
    yield name
    for i in range(1, _MAX_NAME_TRIES):
        yield f"{stem}_{i}{ext}"


def _rename_to_free_name(src_root, src_rel: str, landing_rel: str, name: str) -> str:
    """Land ``src_rel`` under its free name in the landing dir by renaming
    onto each candidate with NOREPLACE: an existing name (a file, a planted
    link) moves on to the next, so the name is reserved, never probed.
    Returns the landed rel beneath ``AGENTS_DIR``. An ``OSError(EXDEV)``
    (another filesystem) propagates untouched for the caller's copy path."""
    for candidate in _free_name_candidates(name):
        dst_rel = f"{landing_rel}/{candidate}" if landing_rel else candidate
        try:
            safe_fs.rename_beneath(
                src_root, src_rel, dst_rel, dst_root=config.AGENTS_DIR, replace=False,
            )
            return dst_rel
        except FileExistsError:
            continue
    raise HTTPException(status_code=409, detail="Too many files with this name")


def _temp_stem(name: str) -> str:
    """The landing name cut to fit a temp name under NAME_MAX, by encoded
    bytes: a multibyte name cut by characters can still overflow the limit."""
    return os.fsencode(name)[:_TEMP_STEM_BYTES].decode("utf-8", "ignore")


def _open_landing_temp(landing_rel: str, name: str, incoming: int):
    """Create the landing dir and a fresh ``.partial`` temp inside it
    (exclusive, never through a link); refuse below the free-disk floor.
    Returns ``(file, temp rel)``. Runs on the file-commit executor."""
    safe_fs.mkdirs_beneath(config.AGENTS_DIR, landing_rel)
    if not _free_disk_ok(config.AGENTS_DIR, incoming):
        raise _insufficient_storage()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(8):
        # `.partial`: the sync manifest skips it and retention reaps an orphan.
        tmp_rel = f"{landing_rel}/.{_temp_stem(name)}.{secrets.token_hex(6)}.partial"
        try:
            fd = safe_fs.open_beneath(config.AGENTS_DIR, tmp_rel, flags, 0o644)
        except FileExistsError:
            continue
        return os.fdopen(fd, "wb"), tmp_rel
    raise OSError(errno.EEXIST, "no free temp name in the landing dir")


def _discard_landing_temp(f, tmp_rel: str) -> None:
    with contextlib.suppress(Exception):
        f.close()
    with contextlib.suppress(OSError):
        safe_fs.unlink_beneath(config.AGENTS_DIR, tmp_rel, missing_ok=True)


def _commit_single_shot(f, tmp_rel: str, landing_rel: str, name: str) -> str:
    """Flush, fsync, close, then reserve the final name. Executor-side."""
    _flush_fsync_close(f)
    return _rename_to_free_name(config.AGENTS_DIR, tmp_rel, landing_rel, name)


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

    upload_dir, agent_dir = await run_db(
        _resolve_upload_destination, user, agent, target_dir, safe_name, create=False,
    )

    # Quick size check via Content-Length header
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > size_cap + 1024:  # small margin for form fields
        raise HTTPException(status_code=413, detail=f"File too large (max {cap_mb} MB)")
    declared = int(content_length) if content_length and content_length.isdigit() else 0

    try:
        landing_rel = _landing_rel(upload_dir)
    except safe_fs.SafeFsError:
        raise HTTPException(status_code=403, detail="Path outside agent directory")

    # Stream into a `.partial` temp beside the landing name, then reserve the
    # name with a no-replace rename: a big upload has a long failure window,
    # a torn final file must never be visible, and the temp is sync-invisible
    # by its suffix. Every disk step runs on the file-commit executor. A
    # confinement refusal after the destination resolved (a component
    # swapped for a link meanwhile) is the same 403 the chunked routes give.
    try:
        f, tmp_rel = await file_commit.run(_open_landing_temp, landing_rel, safe_name, declared)
    except safe_fs.SafeFsError:
        raise HTTPException(status_code=403, detail="Path outside agent directory")
    except OSError as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")
    total_bytes = 0
    landed_rel: str | None = None
    try:
        batch = bytearray()
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > size_cap:
                raise HTTPException(
                    status_code=413, detail=f"File too large (max {cap_mb} MB)"
                )
            batch += chunk
            if len(batch) >= _WRITE_BATCH_BYTES:
                await file_commit.run(_write_batch, f, bytes(batch))
                batch = bytearray()
        if batch:
            await file_commit.run(_write_batch, f, bytes(batch))
        landed_rel = await file_commit.run(
            _commit_single_shot, f, tmp_rel, landing_rel, safe_name,
        )
    except HTTPException:
        raise
    except safe_fs.SafeFsError:
        raise HTTPException(status_code=403, detail="Path outside agent directory")
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")
    finally:
        if landed_rel is None:
            # Any exit without a landed file (a cap, a disk error, a client
            # that went away) leaves no temp behind.
            _discard_landing_temp(f, tmp_rel)

    target = config.AGENTS_DIR / landed_rel
    rel_path = _rel_to_agent_path(landed_rel)
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
# config.UPLOAD_STAGING_DIR, a proxy-private SIBLING of the agents dir,
# one subdirectory per user (a hash of the sub). On bare metal that is the
# same filesystem as the agents tree (the finalize is a rename); in Docker
# the agents dir is a NAMED VOLUME and the staging dir sits in the container
# overlay, so the finalize copies once into the landing dir and renames from
# there. Either way staging sits outside every agent tree, so in-flight
# staging is invisible to sync manifests, agent shells, and the `*.partial`
# retention reaper that patrols AGENTS_DIR. State is disk-first (a meta json
# beside the staging file): status/resume survive a proxy restart with no
# in-memory registry to invalidate.
#
# Bounds: a user holds at most UPLOAD_MAX_OPEN open uploads and
# UPLOAD_STAGING_USER_MB of actually staged bytes (429), the platform at most
# UPLOAD_MAX_OPEN_TOTAL open uploads (503), and no chunk lands below the
# free-disk floor (507). Stale staging is reaped at boot and every ten
# minutes, never on a request.
# ---------------------------------------------------------------------------

# Staging pairs idle beyond this are swept. Meta mtime bumps on every
# received chunk, so only genuinely abandoned uploads age out.
_STAGING_TTL_S = 24 * 3600
# A pair of the caller's own with no chunk in this long gives up its slot when
# the caller is at a per-user cap (the shipped client releases staging on an
# abort or a failure it sees, but a closed tab or a reload leaves the pair,
# which would otherwise hold the slot for the TTL).
_IDLE_EVICT_S = 600
_SWEEP_EVERY_S = 600

# Serializes the staging write + meta update per upload id, and the whole
# complete. The shipped client PUTs strictly sequentially; the lock is
# insurance against a misbehaving caller racing two PUTs into a
# read-modify-write meta loss, and it makes a duplicate complete find the
# staging gone instead of copying twice.
_chunk_locks: dict[str, asyncio.Lock] = {}
# The admission at init (count, check, create) is one critical section in
# the file-commit thread: two inits of one user cannot both pass a cap.
_admission_lock = threading.Lock()


class ChunkedInitRequest(BaseModel):
    agent: str
    filename: str
    size: int
    target_dir: str = ""


# token_urlsafe(16) → 22 chars of [A-Za-z0-9_-]; anything else in the path
# param is hostile (upload_id feeds filesystem paths — this regex is the
# traversal guard, not just tidiness; the joins restate it in barrier shape).
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _user_staging_dirname(sub: str) -> str:
    """One staging subdirectory per user, named by a hash of the sub (subs are
    opaque ids and never become directory names)."""
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()[:32]


def _staging_paths(upload_id: str, sub: str) -> tuple[Path, Path]:
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=404, detail="Unknown upload id")
    d = join_under(config.UPLOAD_STAGING_DIR, _user_staging_dirname(sub))
    return join_under(d, f"{upload_id}.partial"), join_under(d, f"{upload_id}.json")


def _staging_root() -> Path:
    """The safe_fs root the staging tree hangs from: staging is a subtree of
    the platform data dir, never a root of its own."""
    return config.UPLOAD_STAGING_DIR.parent


def _load_chunk_meta(upload_id: str, user: UserContext) -> tuple[dict, Path, Path]:
    """Meta for an in-flight chunked upload, owner- and access-checked.

    404 for an unknown id (or a swept meta); 403 when the caller isn't the
    user who ran init. Re-runs the agent-access check — access may have been
    revoked mid-upload.
    """
    staging, meta_path = _staging_paths(upload_id, user.sub)
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


def _occupancy(meta: dict) -> int:
    """The bytes an open upload actually holds on disk (every received chunk
    is exactly chunk_size but the last), never its declared size."""
    try:
        size = int(meta.get("size", 0))
        chunk_size = int(meta.get("chunk_size", 0))
        received = len(meta.get("received") or [])
    except (TypeError, ValueError):
        return 0
    return max(0, min(received * chunk_size, size))


def _open_metas(user_dir: Path) -> list[tuple[Path, dict, float]]:
    """``(meta path, meta, mtime)`` for every open upload in a user dir."""
    out: list[tuple[Path, dict, float]] = []
    try:
        entries = list(user_dir.iterdir())
    except OSError:
        return out
    for p in entries:
        if p.suffix != ".json":
            continue
        try:
            out.append((p, json.loads(p.read_text()), p.stat().st_mtime))
        except (OSError, ValueError):
            continue
    return out


def _evict_idle(opened: list[tuple[Path, dict, float]]) -> list[tuple[Path, dict, float]]:
    """Remove the oldest of the caller's pairs that received no chunk in the
    idle window; returns the list without it (unchanged when none is idle)."""
    cutoff = time.time() - _IDLE_EVICT_S
    idle = sorted((m for m in opened if m[2] < cutoff), key=lambda m: m[2])
    if not idle:
        return opened
    meta_path, _meta, _mtime = idle[0]
    meta_path.with_suffix(".partial").unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return [m for m in opened if m[0] != meta_path]


def _count_open_total(root: Path) -> int:
    n = 0
    try:
        for d in root.iterdir():
            if not d.is_dir():
                continue
            try:
                n += sum(1 for p in d.iterdir() if p.suffix == ".json")
            except OSError:
                continue
    except OSError:
        return 0
    return n


def _admit_and_stage(sub: str, upload_id: str, meta: dict) -> None:
    """Open one chunked upload for ``sub`` (file-commit thread, under the
    admission lock): the staging root and the user dir exist, the free-disk
    floor holds, the per-user caps hold (an idle pair of the caller's is
    evicted first), the global cap holds, and the pair is created."""
    incoming = int(meta["size"])
    with _admission_lock:
        root = config.UPLOAD_STAGING_DIR
        user_dir = root / _user_staging_dirname(sub)
        user_dir.mkdir(parents=True, exist_ok=True)
        if not _free_disk_ok(root, incoming):
            raise _insufficient_storage()

        opened = _open_metas(user_dir)
        max_open = config.UPLOAD_MAX_OPEN
        if max_open > 0 and len(opened) >= max_open:
            opened = _evict_idle(opened)
            if len(opened) >= max_open:
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many open uploads (UPLOAD_MAX_OPEN={max_open}): "
                           "finish or cancel one first",
                    headers={"Retry-After": "60"},
                )
        max_mb = config.UPLOAD_STAGING_USER_MB
        if max_mb > 0:
            cap = max_mb * 1024 * 1024
            if sum(_occupancy(m) for _p, m, _t in opened) + incoming > cap:
                opened = _evict_idle(opened)
                if sum(_occupancy(m) for _p, m, _t in opened) + incoming > cap:
                    raise HTTPException(
                        status_code=429,
                        detail="Staged uploads over the per-user limit "
                               f"(UPLOAD_STAGING_USER_MB={max_mb}): finish or cancel one first",
                        headers={"Retry-After": "60"},
                    )
        max_total = config.UPLOAD_MAX_OPEN_TOTAL
        if max_total > 0 and _count_open_total(root) >= max_total:
            raise HTTPException(
                status_code=503,
                detail="The platform is handling too many uploads at once: try again shortly",
                headers={"Retry-After": "30"},
            )

        staging, meta_path = _staging_paths(upload_id, sub)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        os.close(fd)
        _save_chunk_meta_atomic(meta_path, meta)


def sweep_stale_staging() -> list[str]:
    """Reap abandoned staging (a thread, never a request): a pair idle past
    the TTL, a pair that received no chunk within ``UPLOAD_FIRST_CHUNK_S``,
    an orphan ``.partial``/``.tmp`` past the TTL; the flat root (pre-upgrade
    leftovers) and every user dir. Returns the swept upload ids so the
    caller can release their locks."""
    swept: list[str] = []
    root = config.UPLOAD_STAGING_DIR
    if not root.is_dir():
        return swept
    now = time.time()
    ttl_cutoff = now - _STAGING_TTL_S
    first_chunk_cutoff = now - config.UPLOAD_FIRST_CHUNK_S

    def _reap_dir(d: Path) -> None:
        try:
            entries = [p for p in d.iterdir() if not p.is_dir()]
        except OSError:
            return
        metas = {p.stem for p in entries if p.suffix == ".json"}
        for p in entries:
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if p.suffix == ".json":
                stale = mtime < ttl_cutoff
                if not stale and mtime < first_chunk_cutoff:
                    try:
                        stale = not json.loads(p.read_text()).get("received")
                    except (OSError, ValueError):
                        stale = True
                if stale:
                    p.with_suffix(".partial").unlink(missing_ok=True)
                    p.unlink(missing_ok=True)
                    swept.append(p.stem)
            elif p.suffix in (".partial", ".tmp") and mtime < ttl_cutoff:
                if Path(p.stem).stem not in metas and p.stem not in metas:
                    p.unlink(missing_ok=True)

    try:
        _reap_dir(root)
        for d in root.iterdir():
            if d.is_dir():
                _reap_dir(d)
    except Exception:
        logger.exception("stale chunked-upload staging sweep failed")
    return swept


def release_swept_locks(upload_ids: list[str]) -> None:
    """Drop the chunk locks of swept uploads; a held lock stays for its
    holder, whose PUT answers 410 and drops it."""
    for uid in upload_ids:
        lock = _chunk_locks.get(uid)
        if lock is not None and not lock.locked():
            _chunk_locks.pop(uid, None)


async def staging_sweep_loop() -> None:
    """Reap stale staging at boot and then every ten minutes (started from
    the lifespan; the sweep itself runs on the file-commit executor)."""
    try:
        swept = await file_commit.run(sweep_stale_staging)
        release_swept_locks(swept)
        logger.info("upload staging sweep: %d stale upload(s) reaped at boot", len(swept))
    except Exception:
        logger.exception("upload staging sweep failed at boot")
    while True:
        await asyncio.sleep(_SWEEP_EVERY_S)
        try:
            swept = await file_commit.run(sweep_stale_staging)
            release_swept_locks(swept)
            if swept:
                logger.info("upload staging sweep: %d stale upload(s) reaped", len(swept))
        except Exception:
            logger.exception("upload staging sweep failed")


def staged_bytes_by_user() -> dict[str, int]:
    """Bytes actually staged per user dir (the hashed sub), for the quota view."""
    out: dict[str, int] = {}
    root = config.UPLOAD_STAGING_DIR
    if not root.is_dir():
        return out
    try:
        for d in root.iterdir():
            if d.is_dir():
                out[d.name] = sum(_occupancy(m) for _p, m, _t in _open_metas(d))
    except OSError:
        pass
    return out


def staged_bytes_total() -> int:
    return sum(staged_bytes_by_user().values())


def staged_bytes_for(sub: str) -> int:
    """Bytes one user has staged (their directory only), for the per-user
    quota bucket."""
    d = config.UPLOAD_STAGING_DIR / _user_staging_dirname(sub)
    if not d.is_dir():
        return 0
    try:
        return sum(_occupancy(m) for _p, m, _t in _open_metas(d))
    except OSError:
        return 0


@router.post("/v1/upload/chunked/init")
async def chunked_upload_init(
    body: ChunkedInitRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Open a chunked upload: validate cap + destination, admit, create staging.

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
    await run_db(
        _resolve_upload_destination, user, body.agent, body.target_dir, safe_name,
        create=False,
    )

    allowed, retry_after = rate_limiter.hit("upload_init", user.sub)
    if not allowed:
        raise HTTPException(
            status_code=429, detail="Too many uploads started: try again later",
            headers={"Retry-After": str(max(1, retry_after))},
        )

    upload_id = secrets.token_urlsafe(16)
    chunk_size = config.UPLOAD_CHUNK_BYTES
    meta = {
        "sub": user.sub,
        "agent": body.agent,
        "target_dir": body.target_dir,
        "filename": safe_name,
        "size": body.size,
        "chunk_size": chunk_size,
        "received": [],
    }
    await file_commit.run(_admit_and_stage, user.sub, upload_id, meta)
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


def _open_chunk_target(staging: Path, offset: int, incoming: int):
    """Open the staging file at the chunk's offset; refuse below the floor.
    Executor-side."""
    if not _free_disk_ok(config.UPLOAD_STAGING_DIR, incoming):
        raise _insufficient_storage()
    fd = os.open(staging, os.O_RDWR | os.O_NOFOLLOW)
    f = os.fdopen(fd, "r+b")
    f.seek(offset)
    return f


def _commit_chunk(f, meta_path: Path, meta: dict) -> None:
    """Flush, fsync, close, then record the chunk. Executor-side. A 2xx is a
    durability promise the client's resume logic relies on."""
    _flush_fsync_close(f)
    _save_chunk_meta_atomic(meta_path, meta)


def _gone(upload_id: str, meta_path: Path) -> HTTPException:
    # Meta survived but staging was swept (TTL): unrecoverable.
    meta_path.unlink(missing_ok=True)
    _chunk_locks.pop(upload_id, None)
    return HTTPException(
        status_code=410, detail="Upload staging expired: restart the upload",
    )


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
        raise _gone(upload_id, meta_path)

    size = int(meta["size"])
    chunk_size = int(meta["chunk_size"])
    n_chunks = (size + chunk_size - 1) // chunk_size
    if index < 0 or index >= n_chunks:
        raise HTTPException(status_code=400, detail="Chunk index out of range")
    expected = min(chunk_size, size - index * chunk_size)

    # The lock is minted after the checks above; an upload whose first PUT
    # fails gives it back below. The meta is read again under the lock: a
    # sibling chunk's commit between the check and this write would
    # otherwise be dropped from ``received`` by this writer's stale copy.
    had_chunks = bool(meta.get("received"))
    lock = _chunk_locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        meta, staging, meta_path = _load_chunk_meta(upload_id, user)
        received = 0
        f = None
        try:
            f = await file_commit.run(_open_chunk_target, staging, index * chunk_size, expected)
            batch = bytearray()
            async for part in request.stream():
                if not part:
                    continue
                received += len(part)
                if received > expected:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Chunk {index} larger than expected ({expected} bytes)",
                    )
                batch += part
                if len(batch) >= _WRITE_BATCH_BYTES:
                    await file_commit.run(_write_batch, f, bytes(batch))
                    batch = bytearray()
            if batch:
                await file_commit.run(_write_batch, f, bytes(batch))
            if received != expected:
                raise HTTPException(
                    status_code=400,
                    detail=f"Chunk {index} size mismatch (got {received}, expected {expected})",
                )
            if index not in meta["received"]:
                meta["received"] = sorted([*meta["received"], index])
            await file_commit.run(_commit_chunk, f, meta_path, meta)
            f = None
        except HTTPException:
            if not had_chunks:
                _chunk_locks.pop(upload_id, None)
            raise
        except Exception as e:
            logger.error(f"Chunk write failed ({upload_id}/{index}): {e}", exc_info=True)
            if not had_chunks:
                _chunk_locks.pop(upload_id, None)
            raise HTTPException(status_code=500, detail="Chunk write failed")
        finally:
            if f is not None:
                with contextlib.suppress(Exception):
                    f.close()
    return {"received": len(meta["received"]), "total": n_chunks}


def _finalize_staged_file(
    staging: Path, landing_rel: str, name: str, upload_id: str, size: int,
    meta_path: Path,
) -> str:
    """Land the assembled staging file under its free name, atomically, and
    drop its meta. Executor-side. Returns the landed rel beneath AGENTS_DIR.

    Fast path: a no-replace rename (staging is a sibling of the agents dir
    on a bare-metal install). Across filesystems (Docker: the agents dir is
    a named volume and staging sits in the container overlay, so the rename
    raises EXDEV) the file is copied ONCE into a ``.partial`` temp beside
    the landing name and the same rename loop runs within the agent tree, so
    the destination is still never observable half-written and a planted
    link at the landing name is never written through.
    """
    staging_root = _staging_root()
    staging_rel = safe_fs.rel_under(staging, staging_root)
    safe_fs.mkdirs_beneath(config.AGENTS_DIR, landing_rel)
    try:
        landed = _rename_to_free_name(staging_root, staging_rel, landing_rel, name)
    except OSError as exc:
        if exc.errno != errno.EXDEV or isinstance(exc, safe_fs.SafeFsError):
            raise
        if not _free_disk_ok(config.AGENTS_DIR, size):
            raise _insufficient_storage()
        tmp_rel = f"{landing_rel}/.{_temp_stem(name)}.{upload_id}.partial"
        safe_fs.copy_file_beneath(
            staging_root, staging_rel, config.AGENTS_DIR, tmp_rel,
            mode=0o644, exclusive=True, fsync=True,
        )
        try:
            landed = _rename_to_free_name(config.AGENTS_DIR, tmp_rel, landing_rel, name)
        except BaseException:
            with contextlib.suppress(OSError):
                safe_fs.unlink_beneath(config.AGENTS_DIR, tmp_rel, missing_ok=True)
            raise
        with contextlib.suppress(OSError):
            safe_fs.unlink_beneath(staging_root, staging_rel, missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return landed


@router.post("/v1/upload/chunked/{upload_id}/complete")
async def chunked_upload_complete(
    upload_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Assemble-finish: verify, authorize AGAIN, land atomically.

    Re-runs the full destination chain: the caller's role may have changed
    since init, and the conflict-free final name is reserved at the moment
    the file actually lands. Holds the upload's lock for the whole complete
    (a duplicate finds the staging gone and answers 404). Returns the exact
    same shape as ``POST /v1/upload`` so callers can't tell the paths apart.
    """
    user = require_auth(user)
    _load_chunk_meta(upload_id, user)
    lock = _chunk_locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        meta, staging, meta_path = _load_chunk_meta(upload_id, user)
        if not staging.exists():
            raise _gone(upload_id, meta_path)

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
            # mismatch means torn staging: drop it so the client restarts clean.
            staging.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            _chunk_locks.pop(upload_id, None)
            raise HTTPException(
                status_code=409, detail="Assembled size mismatch: restart the upload",
            )
        # The cap may have been LOWERED between init and complete: re-check.
        size_cap = config.MAX_UPLOAD_SIZE_BYTES
        if size > size_cap:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {size_cap // (1024 * 1024)} MB)",
            )

        upload_dir, _agent_dir = await run_db(
            _resolve_upload_destination, user, meta["agent"], meta.get("target_dir", ""),
            meta["filename"], create=False,
        )
        try:
            landing_rel = _landing_rel(upload_dir)
        except safe_fs.SafeFsError:
            raise HTTPException(status_code=403, detail="Path outside agent directory")
        try:
            landed_rel = await file_commit.run(
                _finalize_staged_file, staging, landing_rel, meta["filename"],
                upload_id, size, meta_path,
            )
        except HTTPException:
            raise
        except safe_fs.SafeFsError:
            raise HTTPException(status_code=403, detail="Path outside agent directory")
        except OSError as e:
            logger.error(f"Chunked upload finalize failed ({upload_id}): {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Upload failed")
        _chunk_locks.pop(upload_id, None)

    target = config.AGENTS_DIR / landed_rel
    rel_path = _rel_to_agent_path(landed_rel)
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
    """Abort cleanup (idempotent: a repeated DELETE after a sweep is fine).
    Takes the upload's lock, so a DELETE during a complete waits for the
    landing and then finds nothing to remove."""
    user = require_auth(user)
    staging, meta_path = _staging_paths(upload_id, user.sub)
    lock = _chunk_locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            _chunk_locks.pop(upload_id, None)
            return {"ok": True}  # already gone
        if meta.get("sub") != user.sub:
            raise HTTPException(status_code=403, detail="Not your upload")
        staging.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        _chunk_locks.pop(upload_id, None)
    return {"ok": True}
