"""File Upload REST API.

Provides a multipart upload endpoint for files of any type.
Files are saved to the agent's per-user workspace directory.
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi import File as FileParam

import config
from storage import agent_store
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from storage import database as task_store

logger = logging.getLogger("claude-proxy.uploads")
router = APIRouter()

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB (everything but audio/video)
MAX_MEDIA_UPLOAD_SIZE = 250 * 1024 * 1024  # 250 MB (audio + video)

# Audio/video extensions get the larger cap. Kept in sync with the frontend
# AUDIO_EXTENSIONS/VIDEO_EXTENSIONS in dashboard/src/lib/fileTypes.ts.
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

    # Validate agent exists
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

    original_name = file.filename or "unnamed"
    ext = Path(original_name).suffix.lower()

    # Audio/video get the larger cap; everything else stays at 100 MB.
    size_cap = MAX_MEDIA_UPLOAD_SIZE if ext in MEDIA_EXTENSIONS else MAX_UPLOAD_SIZE
    cap_mb = size_cap // (1024 * 1024)

    # Quick size check via Content-Length header
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > size_cap + 1024:  # small margin for form fields
        raise HTTPException(status_code=413, detail=f"File too large (max {cap_mb} MB)")

    # Sanitize filename
    safe_name = _sanitize_filename(original_name)
    if not safe_name:
        safe_name = f"file{ext}"

    # Resolve target directory
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

    upload_dir.mkdir(parents=True, exist_ok=True)

    # Security: ensure resolved path is within agent dir
    resolved_dir = upload_dir.resolve()
    if not str(resolved_dir).startswith(str(agent_dir.resolve())):
        raise HTTPException(status_code=403, detail="Path outside agent directory")

    target = _resolve_conflict(upload_dir / safe_name)

    # Stream file to disk with size enforcement
    total_bytes = 0
    try:
        with open(target, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > size_cap:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail=f"File too large (max {cap_mb} MB)")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        target.unlink(missing_ok=True)
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
    _schedule_upload_push(agent, rel_path, target)

    return {
        "path": rel_path,
        "filename": target.name,
        "size": total_bytes,
    }


def _schedule_upload_push(agent_slug: str, rel_path: str, host_path: Path) -> None:
    """Background ``_push_upload_to_active_remote_sessions`` for a fresh
    upload, registered with the turn-start barrier
    (``core/remote/upload_inflight``). Never raises, never blocks.

    The cheap in-memory candidate gate runs HERE (synchronously) so
    local-only installs — no connected satellite — schedule nothing at all.
    """
    try:
        from services.remote import workspace_fanout
        if not workspace_fanout.has_fanout_candidates(
            agent_slug, rel_path, include_idle=True,
        ):
            return
        from core.remote import upload_inflight

        async def _push() -> None:
            try:
                await _push_upload_to_active_remote_sessions(
                    agent_slug, rel_path, host_path,
                )
            except Exception:
                logger.exception(
                    "Failed to push upload to remote sessions: %s", rel_path,
                )

        upload_inflight.track(agent_slug, _push())
    except Exception:
        logger.exception("Failed to schedule upload push: %s", rel_path)


async def _push_upload_to_active_remote_sessions(
    agent_slug: str, rel_path: str, host_path: "Path",
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
    try:
        content = host_path.read_bytes()
    except OSError as e:
        logger.warning("Cannot read upload %s for satellite push: %s", host_path, e)
        return
    await workspace_fanout.fan_out_write(agent_slug, rel_path, content, include_idle=True)
