"""Tokenized temporary URLs for serving agent-readable image files to
third-party APIs that require a public URL (notably SerpAPI Google Lens,
which does not accept base64 or file uploads).

Flow:
    image-search-mcp ─► POST /v1/images/temp {session_id, abs_path, ttl?}
                       returns {url, expires_in}
    SerpAPI          ─► GET  /v1/images/temp/{token}  (public, token-gated)

Security:
    * The POST creator is authenticated via the same Bearer +
      verify_session_match pattern as every other /v1/hooks/* endpoint, AND
      the requested abs_path is validated to live inside the session's
      agent_dir.
    * The GET serve endpoint is intentionally UNAUTHENTICATED so SerpAPI's
      crawler can fetch it. Defense in depth: 192-bit random token, 5-min
      default TTL (10-min max), single-resolved-path binding at creation.
    * Deployments behind Authentik / Authelia / oauth2-proxy / CF Access
      MUST add ``^/v1/images/temp/[A-Za-z0-9_-]+$`` to their bypass list,
      same as the existing ``^/v1/triggers/(agent|user)/[^/]+/[^/]+$``
      exception.
"""

from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from api.hooks.hooks import _classify_and_pull
from api.sessions.sessions import verify_session_match
from core.session.session_state import get_session_security

logger = logging.getLogger("claude-proxy")

router = APIRouter()


# In-memory token store: {token: (abs_path, expires_at_monotonic)}.
# Process-local — multi-replica deployments would need a shared store, but
# the typical deployment is single-proxy and 5-min TTLs are tolerant of
# pod restarts during the SerpAPI fetch (the MCP just retries).
_temp_image_tokens: dict[str, tuple[Path, float]] = {}

DEFAULT_TTL_SECONDS = 300        # 5 minutes
MAX_TTL_SECONDS = 600            # 10 minutes (hard cap)
MIN_TTL_SECONDS = 30             # 30 seconds (lower bound — shorter is silly)


class TempImageRequest(BaseModel):
    session_id: str
    abs_path: str
    ttl_seconds: int = DEFAULT_TTL_SECONDS


@router.post("/v1/images/temp")
async def create_temp_image_url(
    req: TempImageRequest,
    authorization: str | None = Header(None),
):
    """Mint a short-lived public URL for one specific image file.

    Accepts the same path forms as ``/v1/hooks/*`` endpoints (see
    ``api/hooks._classify_and_pull``):
      1. Real host-absolute path
      2. Agent-relative (``personal-assistant/users/.../foo.png``)
      3. Sandbox-virtual (``/users/<u>/workspace/.../foo.png``) — what stdio
         MCPs running in bwrap naturally produce
    On remote sessions, sandbox-virtual paths trigger a lazy ``pull_through``
    from the satellite into the platform workspace, so the file lands on
    disk where we can serve it.
    """
    verify_session_match(authorization, req.session_id)

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
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {req.abs_path}")

    ttl = max(MIN_TTL_SECONDS, min(req.ttl_seconds, MAX_TTL_SECONDS))
    token = secrets.token_urlsafe(24)  # ~192 bits of entropy
    _temp_image_tokens[token] = (target, time.monotonic() + ttl)

    base = (config.DASHBOARD_PUBLIC_URL or "").rstrip("/")
    if not base:
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
        f"file={target.name}, ttl={ttl}s"
    )
    return {"url": public_url, "expires_in": ttl}


@router.get("/v1/images/temp/{token}")
async def serve_temp_image(token: str):
    """Serve an image previously registered via POST /v1/images/temp.

    Intentionally unauthenticated — external services (SerpAPI) GET this
    URL. Token + TTL + scope validation at creation time is the security
    boundary.
    """
    entry = _temp_image_tokens.get(token)
    if entry is None:
        # Lazy purge of any expired tokens whenever we see a miss — cheap
        # bound on dict growth without a background task.
        _purge_expired()
        raise HTTPException(status_code=404, detail="not found")

    path, expires_at = entry
    if time.monotonic() > expires_at:
        _temp_image_tokens.pop(token, None)
        raise HTTPException(status_code=410, detail="expired")

    if not path.is_file():
        # File was deleted after the token was minted — treat as gone.
        _temp_image_tokens.pop(token, None)
        raise HTTPException(status_code=404, detail="file no longer exists")

    return FileResponse(path)


def _purge_expired() -> None:
    """Drop expired token entries. Called lazily on cache miss."""
    now = time.monotonic()
    expired = [t for t, (_, exp) in _temp_image_tokens.items() if exp < now]
    for t in expired:
        _temp_image_tokens.pop(t, None)
