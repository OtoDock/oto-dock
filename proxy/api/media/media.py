"""Audio/video/file serving + capability-token minting.

`GET /v1/media/{token}` streams a media file with HTTP Range support so
`<video>`/`<audio>` elements can seek (these tags cannot send Authorization
headers). Auth is the session cookie — it rides along on every same-origin
fetch (media elements, download navigations, the Android DownloadManager
handoff) — plus the per-token provenance check in `api.media.access`; the
unguessable token alone is no longer sufficient. `media_kind="file"` rows are
send_file / document-preview downloads (always attachment-forced by the
inline allowlist).

The row's ``abs_path`` was checked at mint, on the file that existed then;
the agent that owns the file keeps writing under that name afterwards. Serve
therefore never re-opens the name: the path is placed under one of three
proxy-owned roots, opened through ``safe_fs`` (no symlink anywhere below the
root), and the response streams the descriptor the check produced
(``FdFileResponse``). A file swapped for a link after the mint is "not
found", whatever it points to.

`POST /v1/media/token` mints a token for a workspace file (authenticated: agent
access + role check), used by the workspace audio/video previews. Chat playback
tokens are minted server-side by the `/v1/hooks/media` hook.
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.media.access import can_serve_token
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from services.infra import safe_fs
from services.media import media_pipeline
from storage.agents import agent_store
from storage import database as task_store
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.media")
router = APIRouter()

# Workspace-minted tokens self-expire (reaped by the TTL sweep). Chat-display
# tokens minted by the hook pass expires_at="" (durable until chat delete).
_WORKSPACE_TOKEN_TTL = 24 * 3600

# The lazy-pull cache for satellite-host files sits inside the agents tree
# (``core/remote/remote_file_flow._host_cache_root``), one folder per session.
HOST_CACHE_SEGMENT = ".remote-host-cache"


class MediaUnserveable(Exception):
    """The row's path is under no serving root or fails its binding; the
    client gets the same 404 as for a missing file."""


class FdFileResponse(FileResponse):
    """A ``FileResponse`` over a descriptor the caller already checked.

    The path handed to Starlette is ``/proc/self/fd/N``, which re-opens the
    very inode ``fd`` refers to, so Range/206, If-Range, HEAD, Content-Length
    and ETag all work as for a named file while no later rename or symlink can
    redirect the bytes. The response OWNS ``fd``: it is closed when the
    response has been sent, on a client disconnect and on an exception alike
    (a ``BackgroundTask`` would be skipped on disconnect). A caller that
    holds a file object hands over ``os.dup(f.fileno())``.
    """

    def __init__(self, fd: int, stat_result: os.stat_result, **kwargs) -> None:
        self._fd = fd
        super().__init__(safe_fs.fd_path(fd), stat_result=stat_result, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        # A server that implemented pathsend would receive the magic link and
        # open it after the close below; uvicorn does not, but never offer it.
        extensions = scope.get("extensions")
        if extensions and "http.response.pathsend" in extensions:
            scope = {**scope, "extensions": {k: v for k, v in extensions.items()
                                             if k != "http.response.pathsend"}}
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)


def _serve_roots() -> tuple[str, str, str]:
    """``(agents, media cache, host media cache)`` as absolute paths."""
    return (
        os.path.abspath(os.fspath(config.AGENTS_DIR)),
        os.path.abspath(os.fspath(media_pipeline._CACHE_DIR)),
        os.path.abspath(os.fspath(media_pipeline._HOST_CACHE_DIR)),
    )


def locate_media_row(info: dict) -> tuple[str, str]:
    """``(root, rel)`` for a ``media_tokens`` row: the closest of the three
    roots holding its ``abs_path`` and the path below it. Under the agents
    root the first segment is bound to the row's agent (a legacy row with
    ``agent=''`` is bound to the slug the path names) or, for a lazy-pull
    cache path, to the row's session. Raises ``MediaUnserveable``."""
    abs_path = (info.get("abs_path") or "").strip()
    roots = _serve_roots()
    try:
        root, rel = safe_fs.split_under(abs_path, roots)
    except OSError as exc:
        raise MediaUnserveable(f"path outside every serving root: {exc}") from None
    if root == roots[0]:
        first, _, rest = rel.partition("/")
        if first == HOST_CACHE_SEGMENT:
            session = rest.partition("/")[0]
            if not session or session != (info.get("session_id") or ""):
                raise MediaUnserveable("host-cache path bound to another session")
        else:
            agent = (info.get("agent") or "").strip()
            if not config.is_safe_agent_name(first) or (agent and first != agent):
                raise MediaUnserveable("agent segment does not match the row")
    return root, rel


def open_media_row(info: dict) -> tuple[int, os.stat_result, str]:
    """``(fd, stat, rel)`` of the regular file a row names, opened without
    following any symlink. Blocking: run it in a thread. ``FileNotFoundError``
    for a missing file (the caller's re-pull / 404), ``SafeFsError`` for a
    link, a FIFO or an escape, ``MediaUnserveable`` for a bad row."""
    root, rel = locate_media_row(info)
    fd, st = safe_fs.open_regular_for_read(root, rel)
    return fd, st, rel


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
    await run_db(task_store.update_media_token_path, token, str(served), mime=mime)
    return served, mime


async def _open_or_repull(token: str, info: dict) -> tuple[int, os.stat_result, str, dict]:
    """The checked descriptor for a row, re-pulling satellite-host media that
    aged out of its cache. Every refusal is the client's 404 (or the 503 for
    an offline machine): a holder learns nothing about what a path points at."""
    try:
        fd, st, rel = await asyncio.to_thread(open_media_row, info)
        return fd, st, rel, info
    except FileNotFoundError:
        pass
    except MediaUnserveable as exc:
        logger.warning("media token %s (%s) refused: %s", token[:8],
                       info.get("media_kind") or "?", exc)
        raise HTTPException(status_code=404, detail="media not found or expired")
    except (safe_fs.SafeFsError, OSError) as exc:
        logger.warning("media token %s (%s) refused: %s", token[:8],
                       info.get("media_kind") or "?", type(exc).__name__)
        raise HTTPException(status_code=404, detail="media not found or expired")
    # Satellite-host (Desktop/Downloads) media isn't retained on the
    # platform: re-pull it from the laptop on demand if it's connected.
    result = await _repull_satellite_media(token, info)
    if result is None:
        if (info.get("origin_path") or "").strip():
            raise HTTPException(
                status_code=503,
                detail="This clip lives on the remote machine, which is "
                       "offline. Reconnect it and try again.",
            )
        raise HTTPException(status_code=404, detail="media file no longer exists")
    served, repulled_mime = result
    info = {**info, "abs_path": str(served), "mime": repulled_mime}
    try:
        fd, st, rel = await asyncio.to_thread(open_media_row, info)
    except (OSError, MediaUnserveable):
        raise HTTPException(status_code=404, detail="media file no longer exists")
    return fd, st, rel, info


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

    def _job() -> dict | None:
        info = task_store.get_media_token(token)
        # Denied == missing (404): don't hand an unauthorized holder an oracle
        # for whether a leaked token is still live.
        if not info or not can_serve_token(info, user):
            return None
        return info

    info = await run_db(_job)
    if info is None:
        raise HTTPException(status_code=404, detail="media not found or expired")
    # display_ui artifact rows share this table but are served ONLY by
    # /v1/ui/{token} (opaque-origin sandbox CSP) — a text/html row rendering
    # inline from THIS route would be same-origin stored XSS.
    if (info.get("media_kind") or "") == "ui":
        raise HTTPException(status_code=404, detail="media not found or expired")
    fd, st, rel, info = await _open_or_repull(token, info)
    try:
        leaf = rel.rsplit("/", 1)[-1]
        mime = info.get("mime") or media_pipeline.guess_media_mime(leaf)
        # nosniff on every media response so the browser honours our declared
        # type instead of sniffing an attacker-shaped body into something
        # executable.
        headers = {"X-Content-Type-Options": "nosniff"}
        if download:
            # Attachment download. The client `fn` is often a caption/title
            # with no extension (e.g. a track name), so keep the saved file's
            # type by falling back to the served file's real suffix or the mime.
            name = fn or leaf
            if not Path(name).suffix:
                name += Path(leaf).suffix or media_pipeline.guess_media_ext(mime)
            return FdFileResponse(fd, st, media_type=mime, filename=name, headers=headers)
        # Inline disposition is an ALLOWLIST of known-inert types. Everything
        # else (text/html, XHTML, SVG, XML/XSLT, anything scriptable as a
        # top-level document) is forced to an attachment (an <img>/<video>
        # still renders a downloaded-disposition source; only direct
        # navigation changes).
        inline_ok = (
            mime.startswith(("audio/", "video/"))
            or (mime.startswith("image/") and mime != "image/svg+xml")
            or mime == "application/pdf"
        )
        if not inline_ok:
            return FdFileResponse(fd, st, media_type=mime, filename=leaf, headers=headers)
        # No filename → inline; FileResponse adds Accept-Ranges + 206 handling.
        return FdFileResponse(fd, st, media_type=mime, headers=headers)
    except BaseException:
        os.close(fd)
        raise


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
    from api.agents.agents import safe_agent_path
    agent_dir = config.get_agent_dir(req.agent)

    def _job() -> Path | None:
        if not agent_store.agent_exists(req.agent):
            return None
        file_path, _ = safe_agent_path(agent_dir, req.agent, req.path, u, writing=False)
        return file_path

    file_path = await run_db(_job)
    if file_path is None:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")
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
    await run_db(
        task_store.create_media_token,
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
