"""Session lifecycle callbacks: tool results, stop, subagents, file-written
fan-out, the permission response bridge and the location request bridge.

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import logging
import uuid

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from storage import database as task_store
from api.sessions.sessions import verify_api_key_async, verify_session_match_async
from core.session.session_state import (
    _sessions,
    _dashboard_notify_queues,
    record_hook_activity,
    get_permission_queue,
    resolve_permission,
    get_permission_request_session,
    get_session_security,
    push_pump_event,
    wait_for_location,
    get_subagent_registry,
    mark_subagent_done,
)
import contextlib
from api.hooks import routing
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")
router = APIRouter()


class HookToolResultRequest(BaseModel):
    session_id: str
    tool_name: str
    # Exact correlation key from the PostToolUse hook input (empty on older
    # CLIs) — lets the pump attach results to the right block when several
    # same-name tools run in parallel, and Agent reports to their task_spawn.
    tool_use_id: str = ""
    summary: str
    result_content: str = ""
    # Whether the tool call failed — the MCP cost engine skips charging for
    # failed calls (see stream_pump TOOL_RESULT handler).
    is_error: bool = False
    # Interactive TUI mcp__ calls: feed the session allow-memory only, no
    # chat rendering (the terminal already rendered the result).
    memory_only: bool = False
    # The paths the call's input named (file_path / path / notebook_path) —
    # the turn's record (session_events.post_tool). Empty on older scripts.
    tool_paths: list[str] = []
    # A shell tool's command text (capped by the script) — the record's
    # ``command``, from which a check reads a commit, a push, a build.
    # Empty on older scripts and for every other tool.
    tool_command: str = ""


@router.post("/v1/hooks/tool-result")
async def hook_tool_result(req: HookToolResultRequest, authorization: str | None = Header(None)):
    """Called by PostToolUse hook to push a tool result summary to the chat."""
    await verify_session_match_async(authorization, req.session_id)

    # (Removed: the interactive allow-memory inference. It seeded
    # remember_session_tool_allow for any mcp__ tool that RAN in an
    # interactive session, on the premise that execution proved the user
    # clicked Allow in a PLATFORM prompt. Post-flip the platform defers the
    # residual tier to the CLI, so a tool that ran may reflect a native
    # "allow once" — feeding OUR memory from it would hard-allow every later
    # call and suppress the CLI's own re-prompt. The CLI's own allow-memory
    # ("don't ask again") now owns interactive persistence.)
    if req.memory_only:
        # A terminal's transcript tailer records the call (one source per
        # placement — HOOKS.md).
        return {"status": "ok"}

    # The headless Claude placement's ONE post_tool source is this forwarder.
    from core.session import session_events
    session_events.post_tool(
        req.session_id, req.tool_name, tool_use_id=req.tool_use_id,
        tool_input={"_paths": list(req.tool_paths)} if req.tool_paths else None,
        is_error=req.is_error, source="forwarder", command=req.tool_command,
    )

    queue = get_permission_queue(routing.resolve_hook_route(req.session_id).queue_session_id)
    await queue.put({
        "event_type": wire.ITEM_TOOL_RESULT,
        "tool_name": req.tool_name,
        "tool_use_id": req.tool_use_id,
        "summary": req.summary,
        "result_content": req.result_content,
        "is_error": req.is_error,
    })
    return {"status": "ok"}


class HookStopRequest(BaseModel):
    session_id: str
    transcript_path: str = ""
    hook_event_name: str = "Stop"
    # Both CLIs set this once a continue verdict was delivered this turn.
    stop_hook_active: bool = False
    # The agent's final message (capped by the script) — for a turn_end
    # handler that judges the answer, not only the files.
    last_assistant_message: str = ""


@router.post("/v1/hooks/stop")
async def hook_stop(req: HookStopRequest, authorization: str | None = Header(None)):
    """Called by the Stop hook at turn end, on both engines.

    Two jobs (HOOKS.md "Shapes per event"):

    * A Claude INTERACTIVE session (PTY-backed, no pump) has no other
      turn-end signal + transcript pointer, so the proxy reads the JSONL at
      ``transcript_path`` and appends new user/assistant messages to
      chat_messages. A Codex terminal's rollout tailer does its own reading;
      headless sessions are persisted by the pump.
    * The platform's ``turn_end`` verdict: for a TERMINAL session (the CLI
      drives the turn) the hook is the one point that can hold the turn
      open, so the verdict is answered here as ``{"decision": "block",
      "reason": …}`` and the script prints it; for a proxy-driven session
      the hook is an observation and the layer's loop delivers the verdict.
    """
    await verify_session_match_async(authorization, req.session_id)
    record_hook_activity(req.session_id)

    from core.session import interactive_session, session_events
    sess = interactive_session.get(req.session_id)
    stats: dict = {}
    # The session's own tailer (the engine's ``transcript_tailer()``) says
    # whether the hook's transcript pointer is for it: Claude's tailer takes
    # the CLI's session JSONL here (``tail_transcript``); Codex's rollout
    # tailer reads on its own and has no such entry point.
    tail = getattr(sess._tailer(), "tail_transcript", None) if sess is not None else None
    if tail is not None:
        # Blocking file read + DB writes → off the event loop.
        stats = await asyncio.to_thread(
            tail, req.session_id, sess.chat_id, req.transcript_path,
        )

    engine = getattr(sess, "transcript_kind", "") if sess is not None else ""
    verdict = await session_events.turn_end(
        req.session_id, source="hook", engine=engine or "",
        driven_by="cli" if sess is not None else "proxy",
        transcript_path=req.transcript_path,
        last_message=req.last_assistant_message,
        stop_hook_active=req.stop_hook_active,
    )
    out: dict = {"status": "ok", "interactive": sess is not None, **stats}
    if verdict.should_continue:
        out["decision"] = "block"
        out["reason"] = verdict.continue_reason
    return out


class HookSubagentRequest(BaseModel):
    session_id: str
    agent_id: str
    agent_type: str = ""
    hook_event_name: str = "SubagentStop"


@router.post("/v1/hooks/subagent")
async def hook_subagent(req: HookSubagentRequest, authorization: str | None = Header(None)):
    """Called by the SubagentStop hook — the deterministic, idle-safe subagent
    completion signal (foreground AND background).

    Marks the agent done in the per-session SubagentRegistry (keyed by the CLI
    ``agent_id`` == ``task_id``), then forwards a per-agent ``bg_agent_done``
    (keyed by the spawning ``tool_use_id``) so the dashboard clears that one
    widget regardless of finish order. When the cohort is complete the
    _bg_agent_monitor (awaiting the registry's event) fires the nudge — this
    handler never blocks on delivery.
    """
    await verify_session_match_async(authorization, req.session_id)
    # SubagentStop counts as hook activity (settle's lost-Stop safety net).
    record_hook_activity(req.session_id)

    from core.session import session_events
    reg = get_subagent_registry(req.session_id)
    # session_events.subagent_stop marks the registry with buffer=True (a
    # Stop that raced ahead of its spawn is parked) and returns True only on
    # the transition to completed — dedups against the stdout
    # task_notification backup and, on Codex, the thread router.
    if not session_events.subagent_stop(req.session_id, req.agent_id, req.agent_type):
        return {"status": "ok", "duplicate": True}

    tool_use_id = reg.tuid_for(req.agent_id)
    # Meeting participants resolve to the meeting's parent chat; normal
    # sessions to their own chat row, with the pump-stamped registry binding
    # as the last resort.
    chat_id = await routing.resolve_hook_chat_id(req.session_id) or reg.chat_id or ""

    # Live state — clear this agent's widget by id (reconnect accuracy).
    if chat_id and tool_use_id:
        mark_subagent_done(chat_id, tool_use_id)

    # Per-agent WS completion. Pump path (turn still streaming → fg agents)
    # first, else the session notify queue (turn ended → bg agents, no pump).
    event = {"type": wire.BG_AGENT_DONE, "tool_use_id": tool_use_id}
    pushed = push_pump_event(chat_id, event) if chat_id else False
    if not pushed:
        nq = _dashboard_notify_queues.get(req.session_id)
        if nq:
            with contextlib.suppress(Exception):
                nq.put_nowait({"type": wire.BG_AGENT_DONE, "tool_use_id": tool_use_id})
    return {"status": "ok"}


class HookFileWrittenRequest(BaseModel):
    session_id: str
    path: str


@router.post("/v1/hooks/file-written")
async def hook_file_written(
    req: HookFileWrittenRequest,
    authorization: str | None = Header(None),
):
    """Called by Docker MCPs (file-tools) after they write a file.

    For remote sessions, this flushes the newly-written platform-cache file
    back to the satellite so subsequent reads (from the agent CLI on the
    satellite, or from display-mcp running there, or from another Docker
    MCP) see the updated content. No-op for local sessions.

    Returns {"ok": bool} indicating whether the push to satellite succeeded.
    Docker MCPs should log but not fail on a False result — the write has
    already happened on the platform side.
    """
    await verify_session_match_async(authorization, req.session_id)
    from core.remote import remote_file_flow
    if not remote_file_flow.is_remote_session(req.session_id):
        return {"ok": True, "local": True}
    # Satellite-host cache paths push back to the original
    # absolute path via the sidecar metadata instead of the agent-tree
    # flush. file-tools posts the agents-relative form; the proxy
    # normalizes back to absolute under AGENTS_DIR before checking.
    abs_form = req.path
    if not abs_form.startswith("/"):
        abs_form = str(config.AGENTS_DIR / abs_form)
    if remote_file_flow.is_host_cache_path(abs_form):
        ok = await remote_file_flow.push_back_host_path(
            req.session_id, abs_form,
        )
        return {"ok": bool(ok), "kind": "satellite_host"}
    rel = req.path.lstrip("/")
    # file-tools posts the SLUG-PREFIXED agents-relative form
    # ("<agent>/users/.../f.png" — the same form the resolve-path hook hands
    # back for reads), but push_back's canonical gate requires the slug-LESS
    # agent-tree rel. Fold the session's OWN slug off (never a blind strip —
    # a foreign slug stays as-is and fails the canonical gate downstream).
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel):
        ctx = get_session_security(req.session_id)
        slug = getattr(ctx, "agent", "") if ctx is not None else ""
        prefix = f"{slug}/"
        if slug and rel.startswith(prefix) and is_canonical_rel_path(rel[len(prefix):]):
            rel = rel[len(prefix):]
    ok = await remote_file_flow.push_back(req.session_id, rel)
    return {"ok": bool(ok)}


class PermissionResponseRequest(BaseModel):
    request_id: str
    approved: bool = True


@router.post("/v1/sessions/{session_id}/permission-response")
async def permission_response(
    session_id: str,
    req: PermissionResponseRequest,
    authorization: str | None = Header(None),
):
    """Called by the pipe function when the user responds to a permission dialog."""
    await verify_session_match_async(authorization, session_id)

    # The session JWT authorizes THIS session only — it must not resolve
    # another session's pending request. (404, not 403, so a guessed
    # request_id can't be probed for existence.)
    bound_sid = get_permission_request_session(req.request_id)
    if bound_sid is not None and bound_sid != session_id:
        raise HTTPException(status_code=404, detail="No pending permission request with that ID")

    found = resolve_permission(req.request_id, req.approved)
    if not found:
        raise HTTPException(status_code=404, detail="No pending permission request with that ID")

    logger.info(f"Permission resolved: session={session_id}, request={req.request_id}, approved={req.approved}")
    return {"status": "ok"}


# --- Location bridge (MCP -> proxy -> WS -> dashboard -> WS -> proxy -> MCP) ---


@router.post("/v1/location/request")
async def request_user_location(
    authorization: str | None = Header(None),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Called by location-mcp to request the user's GPS location via the dashboard.

    Routes to the CALLER'S OWN session (resolved from its session token's
    ``sid``) so a location_request can never be pushed to — or GPS harvested
    from — a DIFFERENT user's dashboard. Only a trusted master-key caller (no
    session sid) falls back to the explicit X-Agent-Name lookup. Then pushes a
    location_request event to that session's WS and blocks for the response.
    """
    await verify_api_key_async(authorization)

    # Resolve the caller's OWN session id from its session token. Empty for a
    # master-key caller (trusted s2s — may use the agent-name fallback below).
    from auth.session_token import validate_session_token
    _token = authorization.split(" ", 1)[1] if authorization and " " in authorization else ""
    caller_sid = (validate_session_token(_token) or {}).get("sid", "")

    if caller_sid:
        latest_sid = caller_sid
    else:
        if not x_agent_name:
            return {"error": "X-Agent-Name header required"}
        # Master-key fallback: most-recently-active non-task session for the agent.
        agent_sessions = [
            (sid, meta) for sid, meta in _sessions.items()
            if meta.get("agent") == x_agent_name and not meta.get("is_task")
        ]
        if not agent_sessions:
            return {"error": "No active dashboard session -- user may not be online"}
        dashboard_sessions = [
            (sid, meta) for sid, meta in agent_sessions if sid in _dashboard_notify_queues
        ]
        pool = dashboard_sessions if dashboard_sessions else agent_sessions
        latest_sid, _ = max(pool, key=lambda x: x[1].get("last_active", ""))

    if latest_sid not in _dashboard_notify_queues:
        return {"error": "No active dashboard session -- user may not be online"}

    # Find chat_id for this session
    chat = await asyncio.to_thread(task_store.get_chat_by_session, latest_sid)
    chat_id = chat["id"] if chat else None

    # Generate request ID and push to dashboard
    request_id = str(uuid.uuid4())
    location_event = {"type": wire.LOCATION_REQUEST, "request_id": request_id}

    # Try pump first (works during streaming), fallback to notify queue
    pushed = push_pump_event(chat_id, location_event) if chat_id else False
    if not pushed:
        notify_queue = _dashboard_notify_queues.get(latest_sid)
        if notify_queue:
            await notify_queue.put({"type": wire.LOCATION_REQUEST, "data": location_event})
        else:
            return {"error": "No active dashboard session -- user may not be online"}

    logger.info(f"Location request: session={latest_sid[:8]}, request_id={request_id}")

    # Block waiting for dashboard response
    result = await wait_for_location(request_id, timeout=30.0, session_id=latest_sid)

    # Optional: reverse geocode for address hint
    if result.get("lat") and not result.get("error"):
        try:
            from storage.identity import credential_store
            maps_creds = await asyncio.to_thread(credential_store.get_infra_credentials, "google-maps")
            api_key = (maps_creds or {}).get("GOOGLE_MAPS_API_KEY", "")
            if api_key:
                async with httpx.AsyncClient(timeout=5) as gc:
                    resp = await gc.get(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        params={"latlng": f"{result['lat']},{result['lng']}", "key": api_key},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("results"):
                            result["address_hint"] = data["results"][0]["formatted_address"]
        except Exception as exc:
            # Non-critical
            logger.debug(f"Reverse geocode for location address hint failed: {exc}")

    return result
