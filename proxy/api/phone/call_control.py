"""``POST /v1/phone/call-control``: the call tools' way to the daemon.

The call-control MCP (and nothing else) calls this with the session's own
JWT. The session is the whole identity: a live token names the call its
binding belongs to, external callers' sessions included (an inbound caller
ends their own call with it), and the body names no session or call. The
registry (``services/phone/call_control.py``) sends the frame and answers
with the daemon's verdict, which the tool shows the model as text.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth.session_token import validate_session_token
from services.phone import call_control
from storage.pg import run_db_fast

logger = logging.getLogger("otodock")
router = APIRouter()

_REASON_MAX = 200
_TARGET_MAX = 80
_QUESTION_MAX = 1000


class CallControlBody(BaseModel):
    action: str
    reason: str = ""
    target: str = ""
    question: str = ""


def _session_id(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    payload = validate_session_token(parts[1])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid API key")
    from core.session.session_state import session_token_refusal
    if session_token_refusal(payload):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return payload


@router.post("/v1/phone/call-control")
async def call_control_endpoint(
    body: CallControlBody,
    authorization: str | None = Header(None),
):
    payload = _session_id(authorization)
    from auth.providers import session_token_holder_ok
    if not await run_db_fast(session_token_holder_ok, payload):
        raise HTTPException(status_code=401, detail="Invalid API key")
    sid = payload.get("sid", "") or ""
    action = (body.action or "").strip()
    if action not in call_control.ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown call action {action!r}")
    if call_control.binding_for(sid) is None:
        raise HTTPException(status_code=409, detail="No live call is bound to this session")
    if action == call_control.ACTION_TRANSFER:
        target = (body.target or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="target is required")
        args = {"target": target[:_TARGET_MAX]}
    elif action == call_control.ACTION_ASK:
        question = (body.question or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        args = {"question": question[:_QUESTION_MAX]}
    else:
        args = {"reason": (body.reason or "").strip()[:_REASON_MAX]}
    result = await call_control.request(sid, action, args)
    logger.info("call control: session %s %s %s", sid[:8], action,
                "accepted" if result.get("ok") else "refused")
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message") or call_control.NO_ANSWER_MESSAGE)
    return {"ok": True, "message": result.get("message", "")}
