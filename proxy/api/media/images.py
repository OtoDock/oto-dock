"""Tokenized temporary URLs for serving agent-readable image files to
third-party APIs that require a public URL (notably SerpAPI Google Lens,
which does not accept base64 or file uploads).

Flow:
    image-search-mcp ─► POST /v1/images/temp {session_id, abs_path, ttl?}
                       returns {url, expires_in}
    SerpAPI          ─► GET  /v1/images/temp/{token}  (public, token-gated)

Security:
    * The POST creator is authenticated via the same Bearer +
      verify_session_match_async pattern as every other /v1/hooks/* endpoint, AND
      the requested abs_path is validated to live inside the session's
      agent_dir.
    * Only raster images are minted (png, jpeg, gif, webp, bmp, tiff): the
      URL lives on the dashboard's own origin, so a scriptable document
      (SVG, HTML, XML) served inline there would run as the viewer. The
      mime comes from that table, never from the file.
    * The bytes are read at mint, beneath the agents root with no symlink
      followed, and the SERVE route hands out that snapshot: it never
      opens a path the agent can still rewrite. The snapshot store is
      capped per file, per session and in total.
    * The GET serve endpoint is intentionally UNAUTHENTICATED so SerpAPI's
      crawler can fetch it. Defense in depth: 192-bit random token, 5-min
      default TTL (10-min max), ``nosniff``, a sandboxing CSP, ``no-store``.
    * Deployments behind Authentik / Authelia / oauth2-proxy / CF Access
      MUST add ``^/v1/images/temp/[A-Za-z0-9_-]+$`` to their bypass list,
      same as the existing ``^/v1/triggers/(agent|user)/[^/]+/[^/]+$``
      exception.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import config
from api.hooks.paths import _classify_and_pull
from api.sessions.sessions import verify_session_match_async
from core.session.session_state import get_session_security
from services.infra import safe_fs

logger = logging.getLogger("claude-proxy")

router = APIRouter()


@dataclass
class _TempImage:
    data: bytes
    mime: str
    expires_at: float      # monotonic
    session_id: str


# In-memory snapshot store. Process-local: multi-replica deployments would
# need a shared store, but the typical deployment is single-proxy and 5-min
# TTLs are tolerant of pod restarts during the SerpAPI fetch (the MCP just
# retries).
_temp_image_tokens: dict[str, _TempImage] = {}

DEFAULT_TTL_SECONDS = 300        # 5 minutes
MAX_TTL_SECONDS = 600            # 10 minutes (hard cap)
MIN_TTL_SECONDS = 30             # 30 seconds (lower bound — shorter is silly)

# Lens accepts these and nothing here can carry script. The suffix decides
# the declared type; the bytes are never sniffed.
RASTER_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class TempImageRequest(BaseModel):
    session_id: str
    abs_path: str
    ttl_seconds: int = DEFAULT_TTL_SECONDS


def _purge_expired() -> None:
    now = time.monotonic()
    for t in [t for t, entry in _temp_image_tokens.items() if entry.expires_at < now]:
        _temp_image_tokens.pop(t, None)


def _store_bytes() -> int:
    return sum(len(entry.data) for entry in _temp_image_tokens.values())


@router.post("/v1/images/temp")
async def create_temp_image_url(
    req: TempImageRequest,
    authorization: str | None = Header(None),
):
    """Mint a short-lived public URL for one specific image file.

    Accepts the same path forms as ``/v1/hooks/*`` endpoints (see
    ``api/hooks/paths.py::_classify_and_pull``):
      1. Real host-absolute path
      2. Agent-relative (``personal-assistant/users/.../foo.png``)
      3. Sandbox-virtual (``/users/<u>/workspace/.../foo.png``) — what stdio
         MCPs running in bwrap naturally produce
    On remote sessions, sandbox-virtual paths trigger a lazy ``pull_through``
    from the satellite into the platform workspace, so the file lands on
    disk where we can serve it.
    """
    await verify_session_match_async(authorization, req.session_id)

    ctx = get_session_security(req.session_id)
    if not ctx:
        raise HTTPException(status_code=403, detail="no security context for session")

    # Sandbox/remote-aware resolution (handles local bwrap + remote satellite).
    try:
        target, resolution = await _classify_and_pull(req.session_id, req.abs_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid path: {e}")
    if target is None:
        if resolution is not None and not resolution.allowed:
            raise HTTPException(status_code=403, detail=resolution.error)
        raise HTTPException(status_code=404, detail=f"file not found: {req.abs_path}")
    target = target.resolve(strict=False)

    # Final security check: resolved disk path MUST live under this agent's
    # platform agent_dir. The hook resolver returns the original path on miss,
    # so even if translation failed we still reject anything outside scope.
    agent_dir = config.get_agent_dir(ctx.agent).resolve()
    try:
        target.relative_to(agent_dir)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail=(
                f"resolved path '{target}' is outside the session's agent "
                f"directory ({agent_dir}) — sandbox translation failed or "
                f"path is out of scope"
            ),
        )
    mime = RASTER_MIME.get(target.suffix.lower())
    if mime is None:
        raise HTTPException(
            status_code=415,
            detail="only raster images (png, jpeg, gif, webp, bmp, tiff) can be "
                   "served on a temporary URL",
        )

    # Snapshot the bytes now: the resolved path was authorized, so it is the
    # path opened, beneath the agents root with no symlink followed.
    try:
        rel = safe_fs.rel_under(target, config.AGENTS_DIR)
        data = await asyncio.to_thread(
            safe_fs.read_bytes_beneath, config.AGENTS_DIR, rel,
            max_size=config.TEMP_IMAGE_MAX_BYTES,
        )
    except safe_fs.FileTooLarge:
        raise HTTPException(
            status_code=413,
            detail=f"image is larger than {config.TEMP_IMAGE_MAX_BYTES // (1024 * 1024)} MB",
        )
    except OSError:
        raise HTTPException(status_code=404, detail=f"file not found: {req.abs_path}")

    _purge_expired()
    live = sum(1 for e in _temp_image_tokens.values() if e.session_id == req.session_id)
    if live >= config.TEMP_IMAGE_MAX_PER_SESSION:
        raise HTTPException(
            status_code=429,
            detail=f"this session already has {live} temporary image URLs live; "
                   "wait for one to expire",
        )
    if _store_bytes() + len(data) > config.TEMP_IMAGE_MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=503,
            detail="the temporary image store is full; try again later",
        )

    ttl = max(MIN_TTL_SECONDS, min(req.ttl_seconds, MAX_TTL_SECONDS))
    token = secrets.token_urlsafe(24)  # ~192 bits of entropy
    _temp_image_tokens[token] = _TempImage(
        data=data, mime=mime, expires_at=time.monotonic() + ttl,
        session_id=req.session_id,
    )

    base = (config.DASHBOARD_PUBLIC_URL or "").rstrip("/")
    if not base:
        _temp_image_tokens.pop(token, None)
        raise HTTPException(
            status_code=500,
            detail=(
                "DASHBOARD_PUBLIC_URL not configured; cannot mint a public "
                "URL. Set DASHBOARD_PUBLIC_URL in config.env to enable "
                "external reverse-image-search."
            ),
        )
    public_url = f"{base}/v1/images/temp/{token}"

    logger.info(
        f"Images temp URL minted: session={req.session_id}, "
        f"file={Path(rel).name}, ttl={ttl}s"
    )
    return {"url": public_url, "expires_in": ttl}


@router.get("/v1/images/temp/{token}")
async def serve_temp_image(token: str):
    """Serve an image previously registered via POST /v1/images/temp.

    Intentionally unauthenticated — external services (SerpAPI) GET this
    URL. Token + TTL + scope validation at creation time is the security
    boundary; the bytes are the mint-time snapshot.
    """
    entry = _temp_image_tokens.get(token)
    if entry is None:
        # Lazy purge of any expired tokens whenever we see a miss — cheap
        # bound on dict growth without a background task.
        _purge_expired()
        raise HTTPException(status_code=404, detail="not found")

    if time.monotonic() > entry.expires_at:
        _temp_image_tokens.pop(token, None)
        raise HTTPException(status_code=410, detail="expired")

    # The CSP sandbox is the backstop: should a scriptable type ever slip
    # through the table, a top-level navigation still lands on an inert,
    # opaque origin.
    return Response(
        content=entry.data,
        media_type=entry.mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "no-store",
        },
    )
