"""Session management endpoints -- models, helpers, health, and a Direct
LLM session's abort and close.

Also exports `verify_api_key_async` and `verify_session_match_async` for use by the
`api.hooks` pieces.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException

import config
from auth.providers import UserContext, get_current_user, require_admin
from core.layers.direct import close_direct_session
from core.session.session_state import get_pending_result
from storage.agents import agent_store

logger = logging.getLogger("otodock")
router = APIRouter()


# --- Auth ---

# A session token names the person it was minted for (``user_sub``); that
# person may be gone, or their credentials changed, since the mint. The
# routes here judge that on the fast lane through ``auth.token_holder``,
# whose answer cache the hook routes and the satellite tunnel share.
from auth import token_holder


def _bearer_payload(authorization: str | None, *, accept_callback: bool = False) -> dict | None:
    """The session token's payload, or None for the master key
    (service-to-service: Docker MCPs, the standalone scheduler, phone).
    401 for anything else. ``accept_callback``: a Docker tool's callback
    token too (the five file callbacks, which the tool calls with the token
    its session's config handed it)."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1]
    if config.is_master_key(token):
        return None
    from auth.session_token import validate_callback_token, validate_session_token
    payload = validate_session_token(token)
    if not payload and accept_callback:
        payload = validate_callback_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return payload


async def verify_api_key_async(authorization: str | None) -> None:
    """Validate a Bearer token: the master API key, or a session JWT whose
    person still exists and which is current."""
    payload = _bearer_payload(authorization)
    if payload is not None and not await token_holder.holder_ok(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _session_payload(authorization: str | None, session_id: str, *,
                     accept_callback: bool = False) -> dict | None:
    """The token's payload once its ``sid`` matches the caller-supplied
    session id, or None for the master key. An empty caller-supplied
    session id is refused (400), and so is a token with no ``sid`` (403):
    a token bound to session A must never pass against a blank or another
    session id. Every mint site sets a real sid."""
    payload = _bearer_payload(authorization, accept_callback=accept_callback)
    if payload is None:
        return None
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")
    token_sid = payload.get("sid", "")
    # Bytes: a caller-supplied id may carry any character, and a str compare
    # of a non-ASCII one raises instead of answering.
    if not isinstance(token_sid, str) or not token_sid or not hmac.compare_digest(
            token_sid.encode("utf-8", "surrogatepass"),
            session_id.encode("utf-8", "surrogatepass")):
        raise HTTPException(
            status_code=403,
            detail="Session token does not match request session_id",
        )
    return payload


async def verify_session_match_async(authorization: str | None, session_id: str, *,
                                     accept_callback: bool = False) -> None:
    """Validate the token, cross-check its embedded session_id against the
    caller-supplied one (a hook endpoint's body carries a session_id: an
    MCP or a satellite holding a token for session A must not request
    resources for session B) and judge its holder. The master API key
    bypasses the check (service-to-service: Docker MCPs on the platform,
    the phone server, the standalone scheduler). ``accept_callback``: the
    five file callbacks take a Docker tool's callback token as well."""
    payload = _session_payload(authorization, session_id, accept_callback=accept_callback)
    if payload is not None and not await token_holder.holder_ok(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")


# --- Endpoints ---


@router.api_route("/health", methods=["GET", "HEAD"])
async def health():
    # The public liveness, 200 while the server answers: the Docker
    # healthcheck reads the status code, the dashboard's stale-bundle check
    # reads ``build`` (a page without a dashboard socket asks here when it
    # returns to the foreground and reloads once if its own stamp differs;
    # read per request, cached on the file's mtime/size, so a dashboard-only
    # rebuild is seen without a restart). ``state``, ``ready`` and one word
    # per part come from memory (``services/infra/health_parts.py``): this
    # route never waits on the database. HEAD, for uptime probes, gets the
    # GET's status and headers with no body (the server drops it). Nothing
    # else: versions, pins, counts and the loop's telemetry are an admin's to
    # read (``/v1/admin/health``), not an anonymous caller's.
    from services.infra import health_parts
    from static_assets import dashboard_build_id
    return health_parts.answer(dashboard_build_id())


@router.api_route("/health/ready", methods=["GET", "HEAD"])
async def health_ready():
    """The readiness probe: the same body, 200 while the server takes new
    work (the database answers, a compute machine is signed in and nothing
    holds), else 503. For a caller
    that waits before it sends work; Docker's healthcheck reads ``/health``,
    since a drained server is alive and not ready."""
    from fastapi.responses import JSONResponse

    from services.infra import health_parts
    from static_assets import dashboard_build_id
    body = health_parts.answer(dashboard_build_id())
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


@router.get("/v1/admin/health")
async def admin_health(user: UserContext | None = Depends(get_current_user)):
    """The version payload (the admin footer, the fleet and version checks)
    and the loop's telemetry, for a platform admin."""
    require_admin(user)
    from core import drain, log_queue, loop_watchdog
    from core.session.session_manager import get_all_layers
    from services.infra import health_parts
    from storage import pg
    from ws.satellite import MIN_SATELLITE_VERSION
    return {
        "status": "ok",
        "service": "otodock",
        "version": config.PINNED_OTODOCK_VERSION,
        # The two named fields are frozen (an API contract); cli_versions
        # carries every registered engine's pin keyed by its binary, so a
        # fourth engine shows here without an edit.
        "claude_cli_version": config.PINNED_CLAUDE_CODE_VERSION,
        "codex_cli_version": config.PINNED_CODEX_VERSION,
        "cli_versions": {
            layer.capabilities.runtime.binary: layer.pinned_cli_version()
            for layer in get_all_layers().values()
            if layer.capabilities.runtime.binary
        },
        "satellite_min_version": MIN_SATELLITE_VERSION,
        # Event-loop stall counters (core/loop_watchdog.py), the log writer's
        # queue (core/log_queue.py), the pools and lanes (storage/pg.py) and
        # the descriptor headroom (how many sockets exhaust the proxy: never
        # in the public answer).
        "loop": loop_watchdog.stats(),
        "log": log_queue.stats(),
        "db": pg.pool_stats(),
        "fds": loop_watchdog.fd_stats(),
        # The planned stop's state and what is working right now (the admin
        # command's ``status`` reads the same block).
        "work": drain.work(),
        # The public health's parts and what stands behind them.
        "parts": health_parts.detail(),
    }


@router.get("/v1/models")
async def list_models(authorization: str | None = Header(None)):
    await verify_api_key_async(authorization)
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "otodock",
            }
            for name in agent_store.get_agent_slugs()
        ],
    }


@router.get("/v1/sessions/{session_id}/pending")
async def get_session_pending(session_id: str, authorization: str | None = Header(None)):
    """Retrieve a pending result from a background-completed session.

    Session-bound: retrieval is destructive (one-time read) and the payload
    carries the session's response text and prompt, so a token for session A
    must not be able to drain session B.
    """
    await verify_session_match_async(authorization, session_id)
    result = get_pending_result(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No pending result")
    return result


@router.post("/v1/sessions/{session_id}/abort")
async def abort_session_endpoint(session_id: str, authorization: str | None = Header(None)):
    """Stop a Direct LLM session the platform holds (its session is closed).
    A session a machine runs is not found here: its Stop goes through its
    chat."""
    await verify_session_match_async(authorization, session_id)
    killed = await close_direct_session(session_id)
    logger.info("Abort request: session=%s, killed=%s", session_id, killed)
    return {"status": "aborted" if killed else "not_found", "session_id": session_id}


@router.delete("/v1/sessions/{session_id}")
async def close_session_endpoint(session_id: str, authorization: str | None = Header(None)):
    """Close a Direct LLM session the platform holds. A session a machine
    runs is not found here: its chat's close ends it."""
    await verify_session_match_async(authorization, session_id)
    closed = await close_direct_session(session_id)
    logger.info("Close session request: session=%s, closed=%s", session_id, closed)
    return {"status": "closed" if closed else "not_found", "session_id": session_id}
