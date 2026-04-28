"""Audio/video/file serving + capability-token minting.

`GET /v1/media/{token}` streams a media file with HTTP Range support so
`<video>`/`<audio>` elements can seek (these tags cannot send Authorization
headers). Auth is the session cookie — it rides along on every same-origin
fetch (media elements, download navigations, the Android DownloadManager
handoff) — plus the per-token provenance check in `api.media.access`; the
unguessable token alone is no longer sufficient. `media_kind="file"` rows are
send_file / document-preview downloads (always attachment-forced by the
inline allowlist).

`POST /v1/media/token` mints a token for a workspace file (authenticated: agent
access + role check), used by the workspace audio/video previews. Chat playback
tokens are minted server-side by the `/v1/hooks/media` hook.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.media.access import can_serve_token
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from services.media import media_pipeline
from storage import agent_store
from storage import database as task_store

logger = logging.getLogger("claude-proxy.media")
router = APIRouter()

# Workspace-minted tokens self-expire (reaped by the TTL sweep). Chat-display
# tokens minted by the hook pass expires_at="" (durable until chat delete).
_WORKSPACE_TOKEN_TTL = 24 * 3600


async def _repull_satellite_media(token: str, info: dict) -> tuple[Path, str] | None:
    """Re-fetch a satellite-host media file from the laptop on replay (the
    session-scoped / TTL'd copy is gone). Returns (served_path, mime), or None
    when there's no origin, the machine is offline, or the pull fails."""
    origin = (info.get("origin_path") or "").strip()
    machine_id = (info.get("machine_id") or "").strip()
    if not origin or not machine_id:
        return None
    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    cm = get_connection_manager()
    if cm.get_connection(machine_id) is None:
        return None  # machine offline → caller surfaces a 503
    host_dir = media_pipeline.host_cache_dir()
    dest = host_dir / f"repull-{token}{Path(origin).suffix}"
    ok = await cm.pull_file_to_path(
        machine_id, PathRef("satellite_host", origin), dest,
    )
    if not ok or not dest.is_file():
        return None
    served, mime, _ = await media_pipeline.ensure_playable_async(
        dest, media_kind=info.get("media_kind", ""), dest_dir=host_dir,
    )
    task_store.update_media_token_path(token, str(served), mime=mime)
    return served, mime


@router.get("/v1/media/{token}")
async def serve_media(
    token: str,
    fn: str = "",
    download: bool = False,
    user: UserContext | None = Depends(get_current_user),
):
    """Stream a media file by capability token (Range-capable, inline by default).

    `?download=1&fn=name.mp4` switches to attachment disposition for the
    download button (the `?fn=` is also read by the Android WebView
    DownloadListener, which can't see Content-Disposition).
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    info = task_store.get_media_token(token)
    # Denied == missing (404): don't hand an unauthorized holder an oracle for
    # whether a leaked token is still live.
    if not info or not can_serve_token(info, user):
        raise HTTPException(status_code=404, detail="media not found or expired")
    # display_ui artifact rows share this table but are served ONLY by
    # /v1/ui/{token} (opaque-origin sandbox CSP) — a text/html row rendering
    # inline from THIS route would be same-origin stored XSS.
    if (info.get("media_kind") or "") == "ui":
        raise HTTPException(status_code=404, detail="media not found or expired")
    path = Path(info["abs_path"])
    if not path.is_file():
        # Satellite-host (Desktop/Downloads) media isn't retained on the
        # platform — re-pull it from the laptop on demand if it's connected.
        result = await _repull_satellite_media(token, info)
        if result is None:
            if (info.get("origin_path") or "").strip():
                raise HTTPException(
                    status_code=503,
                    detail="This clip lives on the remote machine, which is "
                           "offline — reconnect it and try again.",
                )
            raise HTTPException(status_code=404, detail="media file no longer exists")
        path, repulled_mime = result
        info = {**info, "mime": repulled_mime}
    mime = info.get("mime") or media_pipeline.guess_media_mime(path)
    # nosniff on every media response so the browser honours our declared type
    # instead of sniffing an attacker-shaped body into something executable.
    headers = {"X-Content-Type-Options": "nosniff"}
    if download:
        # Attachment download. The client `fn` is often a caption/title with no
        # extension (e.g. a track name), so keep the saved file's type by
        # falling back to the served file's real suffix or the mime.
        name = fn or path.name
        if not Path(name).suffix:
            name += path.suffix or media_pipeline.guess_media_ext(mime)
        return FileResponse(path, media_type=mime, filename=name, headers=headers)
    # Inline disposition is an ALLOWLIST of known-inert types. Everything
    # else — text/html, XHTML, SVG, XML/XSLT, anything scriptable as a
    # top-level document — is forced to an attachment (an <img>/<video> still
    # renders a downloaded-disposition source; only direct navigation changes).
    inline_ok = (
        mime.startswith(("audio/", "video/"))
        or (mime.startswith("image/") and mime != "image/svg+xml")
        or mime == "application/pdf"
    )
    if not inline_ok:
        return FileResponse(path, media_type=mime, filename=path.name, headers=headers)
    # No filename → inline; FileResponse adds Accept-Ranges + 206 handling.
    return FileResponse(path, media_type=mime, headers=headers)


class MintMediaTokenRequest(BaseModel):
    agent: str
    path: str  # agent-relative path (e.g. "users/alice/workspace/clip.mp4")


@router.post("/v1/media/token")
async def mint_media_token(
    req: MintMediaTokenRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Mint a playback token for a workspace media file. Authorized exactly like
    reading the file (`require_agent_access` + `_check_file_role`)."""
    u = require_auth(user)
    require_agent_access(u, req.agent)
    if not agent_store.agent_exists(req.agent):
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")
    from api.agents.agents import safe_agent_path
    agent_dir = config.get_agent_dir(req.agent)
    file_path, _ = safe_agent_path(agent_dir, req.agent, req.path, u, writing=False)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    served_path, mime, cache_owned = await media_pipeline.ensure_playable_async(file_path)
    kind = (
        media_pipeline.media_kind_from_mime(mime)
        or media_pipeline.media_kind_from_path(file_path)
    )
    token = secrets.token_urlsafe(32)
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=_WORKSPACE_TOKEN_TTL)
    ).isoformat()
    task_store.create_media_token(
        token,
        str(served_path),
        mime=mime,
        media_kind=kind,
        chat_id=None,
        session_id="",
        machine_id=None,
        cache_owned=cache_owned,
        expires_at=expires,
        owner_sub=u.sub,
        agent=req.agent,
    )
    return {
        "url": f"/v1/media/{token}",
        "expires_in": _WORKSPACE_TOKEN_TTL,
        "media_kind": kind,
        "mime": mime,
    }
