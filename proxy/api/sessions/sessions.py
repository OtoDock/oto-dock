"""Session management endpoints -- models, helpers, health, file serving, plan files,
warmup, and session control (mode/model/thinking/permission).

Also exports `verify_api_key` and `verify_session_match` for use by `api.hooks.hooks`.
"""

import asyncio
import hmac
import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from storage import agent_store
from auth.path_policy import SecurityContext
from core.session.session_state import (
    _sessions,
    set_session_mode,
    get_pending_result,
    set_session_security,
    _record_session_use,
)
from core.layers.cli import (
    abort_session,
    get_persistent_session,
    get_or_create_persistent_session,
    close_persistent_session,
    interrupt_persistent_session,
)
from core.layers.direct import create_direct_session, close_direct_session

logger = logging.getLogger("claude-proxy")
router = APIRouter()


# --- Auth ---


def verify_api_key(authorization: str | None = Header(None)) -> None:
    """Validate Bearer token — accepts master API key or session JWT."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1]
    # Master key (service-to-service: Docker MCPs, standalone scheduler, phone)
    if config.is_master_key(token):
        return
    # Session-scoped JWT (agent subprocesses: hooks, MCPs)
    from auth.session_token import validate_session_token
    if validate_session_token(token):
        return
    raise HTTPException(status_code=401, detail="Invalid API key")


def verify_session_match(authorization: str | None, session_id: str) -> None:
    """Validate token AND cross-check its embedded session_id against the
    caller-supplied session_id. Used by hook endpoints where the request body
    carries a session_id — prevents an MCP/satellite holding a token for
    session A from requesting resources for session B.

    Master API key bypasses the check (service-to-service: Docker MCPs on
    platform, phone server, standalone scheduler).
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1]
    if config.is_master_key(token):
        return
    from auth.session_token import validate_session_token
    payload = validate_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid API key")
    # An empty caller-supplied session_id must NOT silently pass: drop the
    # `and session_id` clause so a token bound to session A can't be replayed
    # against a blank/other session_id.
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")
    token_sid = payload.get("sid", "")
    if token_sid and not hmac.compare_digest(token_sid, session_id):
        raise HTTPException(
            status_code=403,
            detail="Session token does not match request session_id",
        )


# --- Endpoints ---


@router.get("/health")
async def health():
    # Liveness + version payload (admin footer + fleet/version checks). Static
    # constants read once at import — cheap enough for the 10s Docker healthcheck.
    # community_mcps_version is omitted until it has a runtime source.
    from ws.satellite import MIN_SATELLITE_VERSION
    return {
        "status": "ok",
        "service": "otodock",
        "version": config.PINNED_OTODOCK_VERSION,
        "claude_cli_version": config.PINNED_CLAUDE_CODE_VERSION,
        "codex_cli_version": config.PINNED_CODEX_VERSION,
        "satellite_min_version": MIN_SATELLITE_VERSION,
    }


@router.get("/v1/models")
async def list_models(authorization: str | None = Header(None)):
    verify_api_key(authorization)
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


@router.get("/v1/agents/{name}/config")
async def get_agent_config(name: str, authorization: str | None = Header(None)):
    """Return the built system prompt for an agent.

    Used by the phone server's DirectLLMClient to get agent prompts
    without going through the CLI. Keeps proxy as single source of truth.
    """
    verify_api_key(authorization)

    prompt = config.build_agent_prompt(name)
    if not prompt:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")

    from services.mcp import mcp_registry
    runtime_mcps = mcp_registry.get_agent_mcps(name)

    return {"agent": name, "system_prompt": prompt, "has_mcp_tools": len(runtime_mcps) > 0}


@router.get("/v1/sessions/{session_id}/pending")
async def get_session_pending(session_id: str, authorization: str | None = Header(None)):
    """Retrieve a pending result from a background-completed session.

    Session-bound: retrieval is destructive (one-time read) and the payload
    carries the session's response text and prompt, so a token for session A
    must not be able to drain session B.
    """
    verify_session_match(authorization, session_id)
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
    verify_session_match(authorization, session_id)
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
    verify_session_match(authorization, session_id)
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
    verify_session_match(authorization, session_id)
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
    model: str  # e.g., "claude-sonnet-5", "claude-opus-4-8[1m]"


@router.patch("/v1/sessions/{session_id}/model")
async def change_session_model(
    session_id: str, req: ModelChangeRequest, authorization: str | None = Header(None),
):
    """Change the LLM model mid-session via the CLI control channel."""
    verify_session_match(authorization, session_id)
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
    verify_session_match(authorization, session_id)
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
    verify_session_match(authorization, session_id)
    session = await get_persistent_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.send_control_response(req.request_id, req.approved)
    return {"status": "ok"}


class ImplementPlanRequest(BaseModel):
    plan_path: str  # e.g., "quiet-snuggling-harbor.md" (filename only)
    mode: str = "acceptEdits"
    agent: str = ""  # if empty, look up from current session


@router.post("/v1/sessions/{session_id}/implement-plan")
async def implement_plan(
    session_id: str,
    req: ImplementPlanRequest,
    authorization: str | None = Header(None),
):
    """Close the plan session and start a new one for implementation.

    Replicates Claude Code CLI's "Clear context and accept edits on" flow:
    1. Closes the current persistent session
    2. Creates a new session with the specified permission mode
    3. Returns the new session_id -- dashboard sends first message with plan path
    """
    verify_session_match(authorization, session_id)

    # Look up agent from current session
    agent_name = req.agent or _sessions.get(session_id, {}).get("agent", "")

    # Close current session
    await close_persistent_session(session_id)

    # Create new session
    new_session_id = str(uuid.uuid4())
    agent_prompt = config.build_agent_prompt(agent_name)
    from services.mcp import mcp_registry
    mcp_config, _, _, _, _ = mcp_registry.build_session_mcp_config(agent_name, None)

    await get_or_create_persistent_session(
        new_session_id,
        agent_prompt=agent_prompt,
        mcp_config_path=mcp_config,
        permission_mode=req.mode,
        client_type="dashboard",
        use_native_permissions=True,
    )

    set_session_mode(new_session_id, "auto")
    # REST endpoint uses API key auth -> synthetic admin
    set_session_security(new_session_id, SecurityContext(
        role="admin",
        username="",
        agent=agent_name,
        is_admin_agent=agent_store.is_admin_only(agent_name),
    ))
    _record_session_use(new_session_id, client_type="dashboard", agent=agent_name)

    logger.info(
        f"Implement plan: closed {session_id}, created {new_session_id} "
        f"(agent={agent_name}, mode={req.mode}, plan={req.plan_path})"
    )
    return {
        "status": "ok",
        "new_session_id": new_session_id,
        "plan_path": req.plan_path,
        "agent": agent_name,
        "mode": req.mode,
    }


# --- Plan file endpoints ---


def _get_plans_dir(session_id: str | None) -> Path:
    """Resolve the plans directory for a session.

    Checks the session's persistent .claude/ dir first (sandbox-aware),
    falls back to ~/.claude/plans/ for legacy/non-sandboxed sessions.
    For remote sessions this returns the local cache dir (populated on
    demand via _ensure_remote_plans_cached).
    """
    if session_id:
        from core.session.session_state import get_session_claude_dir
        claude_dir = get_session_claude_dir(session_id)
        if claude_dir:
            plans = Path(claude_dir) / "plans"
            if plans.is_dir():
                return plans
        # Remote session fallback: local cache populated on demand
        remote_info = _get_remote_session_info(session_id)
        if remote_info is not None:
            return _remote_plans_cache_dir(session_id)
    return Path.home() / ".claude" / "plans"


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
    cache = Path(app_config.SESSIONS_DIR) / "remote-plans" / session_id
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
    cached = cache_dir / filename
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
        rel_path = f"users/{username}/.claude/plans/{filename}"
    else:
        rel_path = f"workspace/.claude/plans/{filename}"

    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    cm = get_connection_manager()
    # filename came from the satellite manifest — keep the write inside the
    # plans cache dir (pull_file_to_path trusts its dest, no traversal check).
    try:
        cached.resolve().relative_to(cache_dir.resolve())
    except ValueError:
        return None
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
    from core.remote.satellite_connection import get_connection_manager
    import uuid as _uuid
    cm = get_connection_manager()
    conn = cm.get_connection(info.machine_id)
    if not conn:
        return []

    command_id = str(_uuid.uuid4())
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    cm._pending_acks[command_id] = future
    try:
        await conn.ws.send_text(json.dumps({
            "type": "request_manifest",
            "command_id": command_id,
            "agent_slug": info.agent_name,
        }))
        resp = await asyncio.wait_for(future, timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        return []
    finally:
        cm._pending_acks.pop(command_id, None)

    # Prefix we want: users/{username}/.claude/plans/ or workspace/.claude/plans/
    from core.session.session_state import _session_security
    ctx = _session_security.get(session_id)
    username = getattr(ctx, "username", "") if ctx else ""
    prefix = (
        f"users/{username}/.claude/plans/"
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
    verify_session_match(authorization, session_id or "")
    # Remote session: ask the satellite for its manifest
    if session_id and _get_remote_session_info(session_id) is not None:
        plans = await _list_remote_plans(session_id)
        return {"plans": sorted(plans, key=lambda p: p["modified"], reverse=True)}

    plans_dir = _get_plans_dir(session_id)
    if not plans_dir.is_dir():
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
    verify_session_match(authorization, session_id or "")
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Remote session: pull into local cache first
    if session_id and _get_remote_session_info(session_id) is not None:
        cached = await _ensure_remote_plan_cached(session_id, filename)
        if cached is None or not cached.is_file():
            raise HTTPException(status_code=404, detail="Plan not found")
        return {"content": cached.read_text(), "filename": filename}

    plan_path = _get_plans_dir(session_id) / filename
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
    """
    verify_api_key(authorization)

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
