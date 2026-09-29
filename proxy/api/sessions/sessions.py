"""Session management endpoints -- models, helpers, health, file serving, plan files,
warmup, and session control (mode/model/thinking/permission).

Also exports `verify_api_key` and `verify_session_match` for use by the `api.hooks` pieces.
"""

import asyncio
import hmac
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, require_admin
from storage.agents import agent_store
from storage.pg import run_db_fast
from services.infra.path_confinement import PathOutsideRoot, join_under, resolve_under
from core.session.session_state import (
    set_session_mode,
    get_pending_result,
)
from core.layers.cli import (
    abort_session,
    get_persistent_session,
    get_or_create_persistent_session,
    close_persistent_session,
    interrupt_persistent_session,
)
from core.layers.direct import create_direct_session, close_direct_session
from core import layout

logger = logging.getLogger("claude-proxy")
router = APIRouter()


# --- Auth ---

# A session token names the person it was minted for (``user_sub``); that
# person may be gone, or their credentials changed, since the mint. The
# routes here judge that on the fast lane through the async twins below.
# The synchronous verifiers never read the store themselves: they stand by
# the last answer reached for the token's holder, and a miss passes and
# starts the check on the fast lane, so a token whose person is gone is
# refused once that check has answered. A pass stands for HOLDER_TTL_S;
# a refusal for the token's life (the same token never becomes current
# again), and a full table drops passes before refusals. A person's
# sessions are closed by the offboarding closer, so a stale token rarely
# outlives them.
HOLDER_TTL_S = 60.0
_HOLDER_ANSWERS_MAX = 4096
_holder_answers: dict[tuple[str, int], tuple[bool, float]] = {}
# The holder checks a synchronous miss started, by key, and their tasks.
_holder_pending: set[tuple[str, int]] = set()
_holder_checks: set[asyncio.Task] = set()


def _holder_key(payload: dict) -> tuple[str, int] | None:
    sub = payload.get("user_sub") or ""
    if not sub:
        return None
    iat = payload.get("iat")
    return (sub, iat if isinstance(iat, int) else 0)


def _check_holder_soon(payload: dict, key: tuple[str, int]) -> None:
    """Start the holder check of a synchronous miss on the running loop
    (none outside one); one at a time per key."""
    if key in _holder_pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _holder_pending.add(key)
    task = loop.create_task(_holder_ok(payload))
    _holder_checks.add(task)

    def _done(done: asyncio.Task) -> None:
        _holder_checks.discard(done)
        _holder_pending.discard(key)
        if not done.cancelled() and done.exception() is not None:
            logger.warning("session token holder check failed: %s", done.exception())

    task.add_done_callback(_done)


def _holder_cached(payload: dict) -> bool:
    key = _holder_key(payload)
    if key is None:
        return True
    hit = _holder_answers.get(key)
    if hit is None or hit[1] < time.monotonic():
        _check_holder_soon(payload, key)
        return True
    return hit[0]


def _remember(key: tuple[str, int], ok: bool, payload: dict, now: float) -> None:
    """Record an answer: a pass for HOLDER_TTL_S, a refusal until the token
    expires. A full table drops expired answers, then passes; it is cleared
    only when refusals alone fill it."""
    if len(_holder_answers) >= _HOLDER_ANSWERS_MAX:
        for k in [k for k, (good, until) in _holder_answers.items() if good or until < now]:
            del _holder_answers[k]
        if len(_holder_answers) >= _HOLDER_ANSWERS_MAX:
            _holder_answers.clear()
    if ok:
        _holder_answers[key] = (True, now + HOLDER_TTL_S)
        return
    exp = payload.get("exp")
    left = (exp - time.time()) if isinstance(exp, (int, float)) else 0.0
    _holder_answers[key] = (False, now + max(HOLDER_TTL_S, left))


async def _holder_ok(payload: dict) -> bool:
    key = _holder_key(payload)
    if key is None:
        return True
    now = time.monotonic()
    hit = _holder_answers.get(key)
    if hit is not None and hit[1] >= now:
        return hit[0]
    from auth.providers import session_token_holder_ok
    ok = bool(await run_db_fast(session_token_holder_ok, payload))
    _remember(key, ok, payload, time.monotonic())
    return ok


def _bearer_payload(authorization: str | None) -> dict | None:
    """The session token's payload, or None for the master key
    (service-to-service: Docker MCPs, the standalone scheduler, phone).
    401 for anything else."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1]
    if config.is_master_key(token):
        return None
    from auth.session_token import validate_session_token
    payload = validate_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return payload


def verify_api_key(authorization: str | None = Header(None)) -> None:
    """Validate a Bearer token: the master API key or a session JWT."""
    payload = _bearer_payload(authorization)
    if payload is not None and not _holder_cached(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")


async def verify_api_key_async(authorization: str | None) -> None:
    """``verify_api_key`` that also asks the store whether the token's
    person still exists and the token is current."""
    payload = _bearer_payload(authorization)
    if payload is not None and not await _holder_ok(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")


def require_master_key(authorization: str | None) -> None:
    """Master key ONLY — for the endpoints that mint whole sessions. A
    session JWT (any agent subprocess, incl. a phone caller's) must never be
    able to warm a new session for an arbitrary agent."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not config.is_master_key(parts[1]):
        raise HTTPException(status_code=403, detail="This endpoint requires the service key")


def _session_payload(authorization: str | None, session_id: str) -> dict | None:
    """The token's payload once its ``sid`` matches the caller-supplied
    session id, or None for the master key. An empty caller-supplied
    session id is refused (400), and so is a token with no ``sid`` (403):
    a token bound to session A must never pass against a blank or another
    session id. Every mint site sets a real sid."""
    payload = _bearer_payload(authorization)
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


def verify_session_match(authorization: str | None, session_id: str) -> None:
    """Validate token AND cross-check its embedded session_id against the
    caller-supplied session_id. Used by hook endpoints where the request body
    carries a session_id — prevents an MCP/satellite holding a token for
    session A from requesting resources for session B.

    Master API key bypasses the check (service-to-service: Docker MCPs on
    platform, phone server, standalone scheduler).
    """
    payload = _session_payload(authorization, session_id)
    if payload is not None and not _holder_cached(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")


async def verify_session_match_async(authorization: str | None, session_id: str) -> None:
    """``verify_session_match`` that also asks the store whether the token's
    person still exists and the token is current."""
    payload = _session_payload(authorization, session_id)
    if payload is not None and not await _holder_ok(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")


# --- Endpoints ---


@router.get("/health")
async def health():
    # The public liveness: the Docker healthcheck reads the status code, the
    # dashboard's stale-bundle check reads ``build`` (a page without a
    # dashboard socket asks here when it returns to the foreground and
    # reloads once if its own stamp differs; read per request, cached on the
    # file's mtime/size, so a dashboard-only rebuild is seen without a
    # restart). Nothing else: versions, pins and the loop's telemetry are an
    # admin's to read (``/v1/admin/health``), not an anonymous caller's.
    from static_assets import dashboard_build_id
    return {"status": "ok", "service": "otodock", "build": dashboard_build_id()}


@router.get("/v1/admin/health")
async def admin_health(user: UserContext | None = Depends(get_current_user)):
    """The version payload (the admin footer, the fleet and version checks)
    and the loop's telemetry, for a platform admin."""
    require_admin(user)
    from ws.satellite import MIN_SATELLITE_VERSION
    from core import log_queue, loop_watchdog
    from core.session.session_manager import get_all_layers
    from storage import pg
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
    """Kill the current turn for a session (e.g. user pressed stop).

    Kills the process but keeps the session entry so auto-resume works on
    the next message. Falls back to killing direct/one-shot sessions.
    """
    await verify_session_match_async(authorization, session_id)
    # Try interrupt (kill process, keep session) for persistent sessions
    killed = await interrupt_persistent_session(session_id)
    if not killed:
        # Fall back: try direct session (close it), then one-shot (kill it)
        killed = await close_direct_session(session_id)
    if not killed:
        killed = await abort_session(session_id)
    logger.info(f"Abort request: session={session_id}, killed={killed}")
    return {"status": "aborted" if killed else "not_found", "session_id": session_id}


@router.delete("/v1/sessions/{session_id}")
async def close_session_endpoint(session_id: str, authorization: str | None = Header(None)):
    """Gracefully close a persistent session (e.g. phone hangup, chat closed)."""
    await verify_session_match_async(authorization, session_id)
    # Try direct session first, then CLI persistent session
    closed = await close_direct_session(session_id)
    if not closed:
        closed = await close_persistent_session(session_id)
    logger.info(f"Close session request: session={session_id}, closed={closed}")
    return {"status": "closed" if closed else "not_found", "session_id": session_id}


# --- Session control endpoints (mode/model/thinking/permission) ---


class ModeChangeRequest(BaseModel):
    mode: str  # "default", "acceptEdits", "plan", "dontAsk"


@router.patch("/v1/sessions/{session_id}/mode")
async def change_session_mode(
    session_id: str, req: ModeChangeRequest, authorization: str | None = Header(None),
):
    """Change permission mode mid-session via the CLI control channel.

    Only works for sessions started with use_native_permissions=True (dashboard).
    Valid modes: default, acceptEdits, plan, dontAsk.
    """
    await verify_session_match_async(authorization, session_id)
    session = await get_persistent_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.use_native_permissions:
        raise HTTPException(
            status_code=400,
            detail="Session doesn't use native permissions -- mode changes not supported",
        )
    async with session.lock:
        result = await session.send_control_request("set_permission_mode", mode=req.mode)
    if result.get("subtype") == "error":
        raise HTTPException(status_code=400, detail=result.get("error", "Mode change failed"))
    session.permission_mode = req.mode
    logger.info(f"Session {session_id} mode changed to {req.mode}")
    return {"status": "ok", "mode": req.mode}


class ModelChangeRequest(BaseModel):
    model: str  # e.g., "claude-sonnet-5", "claude-opus-5-5"


@router.patch("/v1/sessions/{session_id}/model")
async def change_session_model(
    session_id: str, req: ModelChangeRequest, authorization: str | None = Header(None),
):
    """Change the LLM model mid-session via the CLI control channel."""
    await verify_session_match_async(authorization, session_id)
    session = await get_persistent_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    async with session.lock:
        result = await session.send_control_request("set_model", model=req.model)
    if result.get("subtype") == "error":
        raise HTTPException(status_code=400, detail=result.get("error", "Model change failed"))
    logger.info(f"Session {session_id} model changed to {req.model}")
    return {"status": "ok", "model": req.model}


class ThinkingRequest(BaseModel):
    max_tokens: int | None = None  # null to disable extended thinking


@router.patch("/v1/sessions/{session_id}/thinking")
async def change_session_thinking(
    session_id: str, req: ThinkingRequest, authorization: str | None = Header(None),
):
    """Set max thinking tokens mid-session via the CLI control channel."""
    await verify_session_match_async(authorization, session_id)
    session = await get_persistent_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    async with session.lock:
        result = await session.send_control_request(
            "set_max_thinking_tokens", max_thinking_tokens=req.max_tokens,
        )
    if result.get("subtype") == "error":
        raise HTTPException(status_code=400, detail=result.get("error", "Thinking change failed"))
    logger.info(f"Session {session_id} thinking tokens set to {req.max_tokens}")
    return {"status": "ok", "max_tokens": req.max_tokens}


class NativePermissionResponse(BaseModel):
    request_id: str
    approved: bool = True


@router.post("/v1/sessions/{session_id}/native-permission")
async def native_permission_response(
    session_id: str,
    req: NativePermissionResponse,
    authorization: str | None = Header(None),
):
    """Answer a native CLI permission prompt (can_use_tool) from the dashboard.

    The session's send_message() yields permission_prompt events when the CLI
    requests tool approval. The dashboard renders an approval dialog and calls
    this endpoint with the user's decision. The response is written directly
    to stdin -- no lock needed since stdin writes are independent of stdout reads.
    """
    await verify_session_match_async(authorization, session_id)
    session = await get_persistent_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.send_control_response(req.request_id, req.approved)
    return {"status": "ok"}


# --- Plan file endpoints ---


def _get_plans_dir(session_id: str | None) -> Path | None:
    """The plans directory of a session: its persistent .claude/ dir's
    ``plans`` (sandbox-aware), or for a remote session the local cache dir
    (filled on demand by ``_ensure_remote_plan_cached``). None when the
    session has none: no session, a session that registered no claude dir,
    or one whose plans folder does not exist yet. The proxy account's own
    home belongs to no session and is never answered."""
    if not session_id:
        return None
    from core.session.session_state import get_session_claude_dir
    claude_dir = get_session_claude_dir(session_id)
    if claude_dir:
        plans = Path(claude_dir) / "plans"
        if plans.is_dir():
            return plans
    if _get_remote_session_info(session_id) is not None:
        return _remote_plans_cache_dir(session_id)
    return None


def _get_remote_session_info(session_id: str):
    """Return RemoteSessionInfo if the session is running remotely, else None."""
    try:
        from core.session.session_manager import _get_remote_layer
        layer = _get_remote_layer()
        if layer is None:
            return None
        return layer._sessions.get(session_id)
    except Exception:
        return None


def _remote_plans_cache_dir(session_id: str) -> Path:
    """Local cache directory for remote plan files (1h TTL — see purge logic)."""
    import config as app_config
    cache = join_under(Path(app_config.SESSIONS_DIR) / "remote-plans", session_id)
    cache.mkdir(parents=True, exist_ok=True)
    return cache


async def _ensure_remote_plan_cached(
    session_id: str, filename: str,
) -> Path | None:
    """Pull a single plan file from the satellite into the local cache.

    Returns the cached path, or None if the pull failed.  Existing cached
    files newer than 1 hour are returned without re-pulling.
    """
    import time as _time
    info = _get_remote_session_info(session_id)
    if info is None:
        return None
    cache_dir = _remote_plans_cache_dir(session_id)
    # filename came from the satellite manifest — keep the write inside the
    # plans cache dir (pull_file_to_path trusts its dest, no traversal check).
    try:
        cached = resolve_under(cache_dir / filename, cache_dir)
    except PathOutsideRoot:
        return None
    if cached.exists() and (_time.time() - cached.stat().st_mtime) < 3600:
        return cached

    # Determine the remote-relative path. Plans live inside the session's
    # .claude/ dir, which the satellite roots at agents/{agent}/{cwd}/.claude/.
    # The ExecutionLayer doesn't expose that path, but the satellite's
    # file_pull handler roots paths at agents/{agent_slug}/, so we need the
    # per-user or workspace relative path. Derive it from the session's
    # security_context + path resolution logic.
    from core.session.session_state import _session_security
    ctx = _session_security.get(session_id)
    username = getattr(ctx, "username", "") if ctx else ""
    if username:
        rel_path = f"{layout.user_rel(username)}/.claude/plans/{filename}"
    else:
        rel_path = f"{layout.WORKSPACE}/.claude/plans/{filename}"

    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    cm = get_connection_manager()
    ok = await cm.pull_file_to_path(
        info.machine_id,
        PathRef("agent_tree", rel_path),
        cached,
        agent_slug=info.agent_name,
    )
    return cached if ok else None


async def _list_remote_plans(session_id: str) -> list[dict]:
    """Ask the satellite for its plans manifest entries.

    Walks the satellite's manifest (via request_manifest) and returns any
    entries inside ``.claude/plans/`` as {filename, modified, size}.
    """
    info = _get_remote_session_info(session_id)
    if info is None:
        return []
    from core.remote.remote_workspace_sync import manifest_request
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    try:
        # The manager's own command path: a paging satellite's frames are
        # joined there before the wait resolves.
        resp = await cm.send_command(
            info.machine_id,
            manifest_request(cm, info.machine_id, info.agent_name),
            timeout=10.0,
        )
    except Exception:
        return []
    if not isinstance(resp, dict):
        return []

    # Prefix we want: users/{username}/.claude/plans/ or workspace/.claude/plans/
    from core.session.session_state import _session_security
    ctx = _session_security.get(session_id)
    username = getattr(ctx, "username", "") if ctx else ""
    prefix = (
        f"{layout.user_rel(username)}/.claude/plans/"
        if username else "workspace/.claude/plans/"
    )
    entries: list[dict] = []
    for entry in resp.get("files", []):
        path = entry.get("path", "")
        if not path.startswith(prefix):
            continue
        filename = path[len(prefix):]
        if "/" in filename or not filename.endswith(".md"):
            continue
        entries.append({
            "filename": filename,
            "modified": entry.get("mtime", 0.0),
            "size": entry.get("size", 0),
        })
    return entries


@router.get("/v1/plans")
async def list_plans(
    authorization: str | None = Header(None),
    session_id: str | None = None,
):
    """List available plan files for a session.

    Session-bound: a session JWT must name (and match) the session whose
    plans it lists — plan files live in per-user session dirs.
    """
    await verify_session_match_async(authorization, session_id or "")
    # Remote session: ask the satellite for its manifest
    if session_id and _get_remote_session_info(session_id) is not None:
        plans = await _list_remote_plans(session_id)
        return {"plans": sorted(plans, key=lambda p: p["modified"], reverse=True)}

    plans_dir = _get_plans_dir(session_id)
    if plans_dir is None or not plans_dir.is_dir():
        return {"plans": []}
    plans = []
    for f in sorted(plans_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix == ".md" and f.is_file():
            plans.append({
                "filename": f.name,
                "modified": f.stat().st_mtime,
                "size": f.stat().st_size,
            })
    return {"plans": plans}


@router.get("/v1/plans/{filename}")
async def get_plan_file(
    filename: str,
    authorization: str | None = Header(None),
    session_id: str | None = None,
):
    """Read a plan file.

    Session-bound like the plan list — see ``list_plans``.
    """
    await verify_session_match_async(authorization, session_id or "")
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Remote session: pull into local cache first
    if session_id and _get_remote_session_info(session_id) is not None:
        cached = await _ensure_remote_plan_cached(session_id, filename)
        if cached is None or not cached.is_file():
            raise HTTPException(status_code=404, detail="Plan not found")
        return {"content": cached.read_text(), "filename": filename}

    plans_dir = _get_plans_dir(session_id)
    if plans_dir is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    try:
        plan_path = resolve_under(plans_dir / filename, plans_dir)
    except PathOutsideRoot:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan_path.is_file() or plan_path.suffix != ".md":
        raise HTTPException(status_code=404, detail="Plan not found")
    return {"content": plan_path.read_text(), "filename": filename}


class WarmupRequest(BaseModel):
    model: str = ""  # agent name (required)
    session_id: str | None = None
    permission_mode: str = "auto"
    llm_mode: str = "proxy"  # "proxy" (CLI) | "direct" (Anthropic API)
    phone_mode: bool = False  # True = exclude visual MCPs (the "phone" client-type)
    call_type: str = ""  # "inbound" | "outbound" (call context injection)
    phone_context_override: str = ""  # per-route extra call context (appended)
    use_native_permissions: bool = False  # True = dashboard native CLI modes


@router.post("/v1/sessions/warmup")
async def warmup_session_endpoint(req: WarmupRequest, authorization: str | None = Header(None)):
    """Pre-create a persistent session without sending a message.

    For llm_mode="proxy": starts Claude CLI subprocess + MCP servers.
    For llm_mode="direct": starts MCP servers only, uses Anthropic API directly.

    Used by phone server during greeting playback so MCP tools are warm
    by the time the user speaks. Returns the session_id for follow-up requests.

    Phone sessions are NOT warmed here any more: a call's identity (the
    route, the caller, the PIN gate) rides the management WebSocket warmup
    (``ws/phone.py``); this legacy HTTP path builds a bare, identity-less
    session, so it refuses ``phone_mode`` outright rather than run a caller
    outside the external-route rules.
    """
    require_master_key(authorization)
    if req.phone_mode:
        raise HTTPException(
            status_code=410,
            detail=(
                "Phone sessions are warmed over the management WebSocket "
                "(/ws/phone); the HTTP warmup carries no caller identity and "
                "is not available for calls."
            ),
        )

    session_id = req.session_id or str(uuid.uuid4())

    # Build call context if this is a phone (call) session
    call_context = ""
    if req.phone_mode and req.call_type:
        from adapters.phone import PhoneAdapter
        call_context = "\n\n" + PhoneAdapter.get_phone_context(call_type=req.call_type)
        if req.phone_context_override:
            call_context += "\n" + req.phone_context_override

    if req.llm_mode == "direct":
        # Direct mode: Anthropic API + MCP servers (no CLI subprocess)
        if not config.ANTHROPIC_API_KEY:
            raise HTTPException(
                status_code=500,
                detail="ANTHROPIC_API_KEY not configured for direct mode",
            )
        # Build system prompt with call context for direct mode
        system_prompt = ""
        if call_context:
            base_prompt = config.build_agent_prompt(req.model) or ""
            system_prompt = base_prompt + call_context
        try:
            session = await create_direct_session(
                session_id=session_id,
                agent_name=req.model,
                phone_mode=req.phone_mode,
                system_prompt=system_prompt,
            )
            logger.info(
                f"Direct warmup ready: {session_id} (agent={req.model}, "
                f"tools={len(session.tools)})"
            )
            return {"status": "ready", "session_id": session_id, "llm_mode": "direct"}
        except Exception as e:
            logger.error(f"Direct warmup failed: {e}")
            raise HTTPException(status_code=500, detail=f"Direct session warmup failed: {e}")

    # Proxy mode: CLI subprocess
    agent_prompt = config.build_agent_prompt(req.model) or ""
    if call_context:
        agent_prompt += call_context
    from services.mcp import mcp_registry
    mcp_config_path, _, _, _, _ = mcp_registry.build_session_mcp_config(req.model, None)

    try:
        await get_or_create_persistent_session(
            session_id=session_id,
            agent_prompt=agent_prompt,
            mcp_config_path=mcp_config_path,
            permission_mode=req.permission_mode,
            use_native_permissions=req.use_native_permissions,
        )
        # For native-permission sessions, hook always allows (native perms gate)
        if req.use_native_permissions:
            set_session_mode(session_id, "auto")
        logger.info(
            f"Warmup session created: {session_id} (model={req.model}, "
            f"native_perms={req.use_native_permissions})"
        )
        return {"status": "ready", "session_id": session_id, "llm_mode": "proxy"}
    except Exception as e:
        logger.error(f"Warmup session failed: {e}")
        raise HTTPException(status_code=500, detail=f"Session warmup failed: {e}")
