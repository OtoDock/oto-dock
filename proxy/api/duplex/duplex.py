"""Full-duplex chat voice — session mint.

``POST /v1/duplex/session {chat_id}`` (dashboard cookie auth) mints the
single-use token for ``/ws/duplex``. Availability and chat access are
enforced HERE so the WS can't be reached by hitting it directly; the bridge
re-checks access at attach (the token may outlive a role change by up to its
TTL, and re-checking is cheap).
"""

import contextlib
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user
from services.media import audio_service, duplex_service, ws_audio_token
from storage import database as task_store

router = APIRouter()


class DuplexSessionRequest(BaseModel):
    chat_id: str


class DuplexPrewarmRequest(BaseModel):
    agent: str
    model: str = ""


# One in-flight detached pre-warm per (user, agent): wake detections can
# double-fire (worker restart, debug harness) and the WS-side dedup doesn't
# cover HTTP. The second call awaits the first spawn instead of stacking one.
_prewarm_inflight: dict[tuple[str, str], asyncio.Task] = {}


@router.post("/v1/duplex/session")
async def mint_duplex_session(
    req: DuplexSessionRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Mint a short-lived token for one full-duplex conversation session."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not req.chat_id:
        raise HTTPException(status_code=422, detail="chat_id is required")
    cap = await asyncio.to_thread(audio_service.duplex_capability)
    if not cap.get("available"):
        raise HTTPException(status_code=503, detail=cap.get("reason") or "unavailable")
    denial = await asyncio.to_thread(
        duplex_service.chat_access_denied_reason, req.chat_id,
        user_sub=user.sub, user_role=user.role, user_agents=user.agents,
    )
    if denial:
        status = 404 if "not found" in denial.lower() else 403
        raise HTTPException(status_code=status, detail=denial)
    max_seconds = 1800
    raw = await asyncio.to_thread(
        task_store.get_platform_setting, "audio_duplex_max_seconds")
    with contextlib.suppress(TypeError, ValueError):
        max_seconds = int(raw) if raw else max_seconds
    return ws_audio_token.create_duplex_token(
        user.sub, chat_id=req.chat_id, max_seconds=max_seconds,
    )


@router.post("/v1/duplex/prewarm")
async def duplex_prewarm(
    req: DuplexPrewarmRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Fire-and-forget session pre-warm at wake-word DETECTION time.

    The wake flow's earcon + SPA navigation + chat-WS connect take ~0.5–1.5 s
    before the normal warmup can start the CLI spawn — this endpoint buys that
    window. Best-effort by design: the spawned session lands in the global
    pre-warm registry and the real warmup claims it by (user, agent, model,
    role, exec_path); a miss is just an unclaimed pre-warm the TTL reaper
    frees. Access rule matches the dashboard: admin, or the agent is added
    to the account.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    agent = (req.agent or "").strip()
    if not agent:
        raise HTTPException(status_code=422, detail="agent is required")
    if user.role != "admin" and agent not in (user.agents or []):
        raise HTTPException(status_code=403, detail="Access denied")

    from storage import database as _db
    from ws.dashboard_warmup import spawn_detached_prewarm

    user_row = await asyncio.to_thread(_db.get_user, user.sub)
    if not user_row:
        raise HTTPException(status_code=401, detail="Unknown user")

    key = (user.sub, agent)
    task = _prewarm_inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(spawn_detached_prewarm(
            agent=agent, user=user_row, user_sub=user.sub,
            requested_model=req.model or "",
        ))
        _prewarm_inflight[key] = task
        task.add_done_callback(
            lambda _t, _k=key: _prewarm_inflight.pop(_k, None))
    try:
        sid = await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
    except asyncio.TimeoutError:
        sid = None  # spawn continues in background — still claimable
    except Exception:
        sid = None
    return {"status": "ok", "session_id": sid}
