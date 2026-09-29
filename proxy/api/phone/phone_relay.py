"""Phone-call relay — phone-mcp's path to the phone daemon's call API.

phone-mcp used to dial the phone daemon directly (``PHONE_SERVER_URL`` +
``PHONE_API_SECRET`` in its agent_env), which required every machine that
runs an agent session to resolve and reach the daemon — remote satellites
couldn't (LAN/DNS), and the telephony secret traveled to every session
machine. These endpoints relay the daemon's call API through the proxy
instead: the MCP calls ``PROXY_URL`` with its session JWT (loopback
locally, the satellite HTTP tunnel remotely — both auto-injected), and the
proxy — the only party that needs daemon reachability — attaches
``PHONE_API_SECRET`` server-side.

Gating: a session JWT alone must not grant calling (every sandboxed
subprocess holds one, and a plain HTTP client inside a session reaches this
route as easily as the tool does), so the relay is where the origination
policy lives:

- the token is a live session token (no ``ext`` claim) whose holder is
  still current, minted for an agent with ``phone-mcp`` enabled;
- an outbound call takes the editor tier of the session's acting role: the
  registered SecurityContext's role, or the person's row when no context
  is live; a token that names neither is refused;
- the daemon body is rebuilt from ``phone_number``, ``task_description``
  and ``instructions``, ``wait`` is always false, and ``route_id`` is the
  route pinned to the agent's phone-mcp instance, checked against the
  route store (this agent's, outbound, enabled; on a user-mode route the
  caller is the tied user), so the daemon never picks a default route;
- per-agent, per-user and daily caps and an optional destination allowlist
  (``config.PHONE_CALL*``), counted before the daemon is called;
- a call id belongs to the ``(agent, principal)`` that placed it: the
  status, wait and answer relays serve no other.
"""

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response

import config
from auth import roles
from auth.session_token import validate_session_token
from storage.pg import run_db, run_db_fast

logger = logging.getLogger("claude-proxy")
router = APIRouter()

# Longest daemon-side wait (mirrors phone-mcp's clamp) — the /wait long-poll
# relay reads for the caller's timeout plus slack.
_MAX_WAIT_S = 360

# The daemon's own body rules (phone/validation.py), applied here first so a
# refused body never reaches it.
_PHONE_RE = re.compile(r"\+?\d{2,20}")
_MAX_TASK = 4000
_MAX_INSTRUCTIONS = 8000
_ANSWER_MAX_CHARS = 4000

_HOUR_S = 3600.0
_DAY_S = 24 * 3600.0
_OWNER_TTL_S = 24 * 3600.0
_OWNER_MAX = 4096
_WINDOWS_MAX = 4096

# Sliding windows of origination times per cap key, and the principal each
# live call id belongs to. Per process: a restart starts the windows afresh
# (the daemon's own 60/min brake resets the same way), and one proxy process
# holds the install-wide window.
_call_windows: dict[str, deque] = {}
_call_owners: dict[str, tuple[str, str, float]] = {}


def _now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class _Caller:
    agent: str
    user_sub: str  # "" for an agent-scope session (task, trigger, meeting)
    sid: str
    role: str      # the acting role: the live context's, else the person's row

    @property
    def principal(self) -> str:
        """Who a call belongs to: the person, or the agent itself ("")."""
        return self.user_sub


def _daemon_headers() -> dict:
    if config.PHONE_API_SECRET:
        return {"Authorization": f"Bearer {config.PHONE_API_SECRET}"}
    return {}


async def _acting_role(sid: str, user_sub: str, agent: str) -> str:
    """The role the session acts with on ``agent``: its registered
    SecurityContext's (every layer registers one before the spawn, so a
    below-editor creator's agent-scope task carries the creator's role);
    the person's row when no context is live; refused when neither exists."""
    from core.session.session_state import get_session_security
    ctx = get_session_security(sid) if sid else None
    role = getattr(ctx, "role", "") if ctx is not None else ""
    if isinstance(role, str) and role:
        return role
    if user_sub:
        from auth.providers import acting_role_of
        return await run_db(acting_role_of, user_sub, agent)
    raise HTTPException(status_code=403, detail="This session is not live on the platform")


async def _require_phone_agent(authorization: str | None) -> _Caller:
    """Bearer auth: a live session token of an agent with phone-mcp."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    payload = validate_session_token(parts[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if payload.get("ext"):
        raise HTTPException(
            status_code=403, detail="External sessions cannot use the phone relay")
    from auth.providers import session_token_holder_ok
    if not await run_db_fast(session_token_holder_ok, payload):
        raise HTTPException(status_code=401, detail="Invalid API key")
    agent = payload.get("agent", "") or ""
    sid = payload.get("sid", "") or ""
    user_sub = payload.get("user_sub", "") or ""

    from services.mcp.mcp_registry import get_agent_mcps
    manifests = await run_db(get_agent_mcps, agent)
    if not any(m.name == "phone-mcp" for m in manifests):
        raise HTTPException(
            status_code=403,
            detail=f"Agent {agent!r} does not have phone-mcp assigned",
        )
    role = await _acting_role(sid, user_sub, agent)
    return _Caller(agent=agent, user_sub=user_sub, sid=sid, role=role)


def _validate_body(body) -> tuple[dict, str]:
    """The three fields the daemon body is rebuilt from, and the route id the
    caller named (checked against the pinned one)."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    phone = body.get("phone_number") or ""
    task = body.get("task_description") or ""
    instructions = body.get("instructions") or ""
    route_id = body.get("route_id") or ""
    if not isinstance(phone, str) or not _PHONE_RE.fullmatch(phone):
        raise HTTPException(
            status_code=400,
            detail="phone_number must be 2-20 digits with an optional leading +")
    if not isinstance(task, str) or not task.strip():
        raise HTTPException(status_code=400, detail="task_description is required")
    if len(task) > _MAX_TASK:
        raise HTTPException(status_code=400, detail="task_description is too long")
    if not isinstance(instructions, str) or len(instructions) > _MAX_INSTRUCTIONS:
        raise HTTPException(status_code=400, detail="instructions are too long")
    if not isinstance(route_id, str):
        raise HTTPException(status_code=400, detail="route_id is invalid")
    return {"phone_number": phone, "task_description": task,
            "instructions": instructions}, route_id


def _check_destination(number: str) -> None:
    prefixes = config.PHONE_CALL_ALLOWED_PREFIXES
    if prefixes and not any(number.startswith(p) for p in prefixes):
        raise HTTPException(
            status_code=403,
            detail="The destination is outside the allowed prefixes "
                   "(PHONE_CALL_ALLOWED_PREFIXES)",
        )


async def _pinned_route(caller: _Caller, named: str) -> str:
    """The outbound route this call runs on: the one pinned to the agent's
    phone-mcp instance, an enabled outbound route of this agent, and on a
    user-mode route the caller must be the tied user."""
    from storage.mcp import mcp_store
    from storage.phone import phone_route_store
    inst = await run_db(
        mcp_store.get_instance_for_agent_env_delivery, "phone-mcp", caller.agent)
    pinned = ((inst or {}).get("field_values") or {}).get("OUTBOUND_ROUTE_ID") or ""
    if not isinstance(pinned, str) or not pinned:
        raise HTTPException(
            status_code=403,
            detail="No outbound route is pinned to this agent's phone-mcp instance "
                   "(set OUTBOUND_ROUTE_ID on the instance)",
        )
    if named and named != pinned:
        raise HTTPException(
            status_code=403, detail="Calls run on the route pinned to this agent's instance")
    route = await run_db(phone_route_store.get_route, pinned)
    if (route is None or route.get("agent") != caller.agent
            or route.get("direction") != "outbound" or not route.get("enabled")):
        raise HTTPException(
            status_code=403,
            detail="The pinned route is not an enabled outbound route of this agent")
    from services.phone import phone_identity
    if phone_identity.route_identity_mode(route) == phone_identity.IDENTITY_MODE_USER:
        tied = route.get("identity_user_sub") or ""
        if not caller.user_sub or caller.user_sub != tied:
            raise HTTPException(
                status_code=403,
                detail="The pinned route runs as its tied user: only that user's "
                       "sessions may place calls on it",
            )
    return pinned


def _cap_keys(caller: _Caller) -> list[tuple[str, int, float, str]]:
    keys = [
        (f"agent-hour:{caller.agent}", config.PHONE_CALLS_PER_AGENT_PER_HOUR,
         _HOUR_S, "PHONE_CALLS_PER_AGENT_PER_HOUR"),
        (f"agent-day:{caller.agent}", config.PHONE_CALLS_PER_AGENT_PER_DAY,
         _DAY_S, "PHONE_CALLS_PER_AGENT_PER_DAY"),
    ]
    if caller.user_sub:
        keys.append((f"user-hour:{caller.user_sub}", config.PHONE_CALLS_PER_USER_PER_HOUR,
                     _HOUR_S, "PHONE_CALLS_PER_USER_PER_HOUR"))
    return keys


def _take_cap_slots(caller: _Caller) -> None:
    """Check every cap, then record the call in every window, with no await
    between: concurrent originations cannot all slip under a cap. 0 = no cap."""
    now = _now()
    windows: list[deque] = []
    for key, limit, window_s, name in _cap_keys(caller):
        if limit <= 0:
            continue
        dq = _call_windows.get(key)
        if dq is None:
            dq = _call_windows[key] = deque()
        while dq and now - dq[0] >= window_s:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = int(window_s - (now - dq[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Call cap reached ({name}={limit}): try again later",
                headers={"Retry-After": str(retry_after)},
            )
        windows.append(dq)
    for dq in windows:
        dq.append(now)
    if len(_call_windows) > _WINDOWS_MAX:
        for key in [k for k, dq in _call_windows.items() if not dq]:
            _call_windows.pop(key, None)


def _remember_owner(resp: Response, caller: _Caller) -> None:
    """Bind the call id the daemon answered to the principal that placed it."""
    try:
        call_id = json.loads(resp.body).get("call_id")
    except Exception:
        return
    if not isinstance(call_id, str) or not call_id:
        return
    now = _now()
    if len(_call_owners) >= _OWNER_MAX:
        for cid in [c for c, (_a, _p, at) in _call_owners.items() if now - at > _OWNER_TTL_S]:
            _call_owners.pop(cid, None)
        while len(_call_owners) >= _OWNER_MAX:
            _call_owners.pop(next(iter(_call_owners)))
    _call_owners[call_id] = (caller.agent, caller.principal, now)


def _require_call_owner(call_id: str, caller: _Caller) -> None:
    entry = _call_owners.get(call_id)
    if (entry is None or entry[0] != caller.agent or entry[1] != caller.principal
            or _now() - entry[2] > _OWNER_TTL_S):
        raise HTTPException(status_code=404, detail="call not found")


def _audit(caller: _Caller, route_id: str, decision: str) -> None:
    # The destination number is not logged: the call log row holds it.
    logger.info(
        "phone relay: agent=%s user=%s route=%s %s",
        caller.agent, (caller.user_sub or "-")[:16], route_id or "-", decision,
    )


async def _relay(method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, read_timeout: float = 30.0) -> Response:
    """Forward one request to the phone daemon and pass the response through."""
    url = f"{config.PHONE_SERVER_URL}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=10.0),
        ) as client:
            resp = await client.request(
                method, url, params=params, json=json_body,
                headers=_daemon_headers(),
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Phone daemon unreachable from the proxy "
                f"({config.PHONE_SERVER_URL}): {e}"
            ),
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@router.post("/v1/phone/calls")
async def relay_make_call(
    request: Request,
    authorization: str | None = Header(None),
):
    """Originate an outbound call (→ daemon ``POST /api/calls``)."""
    caller = await _require_phone_agent(authorization)
    if not roles.can_edit(caller.role):
        _audit(caller, "", "refused: below the editor tier")
        raise HTTPException(
            status_code=403,
            detail="Placing calls needs the editor role or above on this agent")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    fields, named_route = _validate_body(body)
    _check_destination(fields["phone_number"])
    route_id = await _pinned_route(caller, named_route)
    _take_cap_slots(caller)
    _audit(caller, route_id, "placed")
    resp = await _relay(
        "POST", "/api/calls",
        json_body={**fields, "route_id": route_id, "wait": False},
    )
    if 200 <= resp.status_code < 300:
        _remember_owner(resp, caller)
    return resp


@router.get("/v1/phone/calls/{call_id}")
async def relay_call_status(
    call_id: str,
    authorization: str | None = Header(None),
):
    """Call status/result (→ daemon ``GET /api/calls/{id}``)."""
    caller = await _require_phone_agent(authorization)
    _require_call_owner(call_id, caller)
    return await _relay("GET", f"/api/calls/{call_id}")


@router.get("/v1/phone/calls/{call_id}/wait")
async def relay_wait_for_call(
    call_id: str,
    timeout: int = 120,
    authorization: str | None = Header(None),
):
    """Long-poll for call events (→ daemon ``GET /api/calls/{id}/wait``)."""
    caller = await _require_phone_agent(authorization)
    _require_call_owner(call_id, caller)
    timeout = max(1, min(timeout, _MAX_WAIT_S))
    return await _relay(
        "GET", f"/api/calls/{call_id}/wait",
        params={"timeout": str(timeout)},
        read_timeout=timeout + 30.0,
    )


@router.post("/v1/phone/calls/{call_id}/answer")
async def relay_answer_question(
    call_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    """Answer a mid-call [QUESTION:] (→ daemon ``POST /api/calls/{id}/answer``)."""
    caller = await _require_phone_agent(authorization)
    _require_call_owner(call_id, caller)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    answer = body.get("answer") if isinstance(body, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise HTTPException(status_code=400, detail="answer must be a non-empty string")
    return await _relay("POST", f"/api/calls/{call_id}/answer",
                        json_body={"answer": answer[:_ANSWER_MAX_CHARS]})
