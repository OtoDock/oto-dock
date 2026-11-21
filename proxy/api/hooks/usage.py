"""A terminal's own report of its account's windows: ``POST
/v1/hooks/usage-report``, called by ``hooks/usage_report.py``, which the
engine runs as its status line command with what the engine read from the
vendor after each answer of the model.

The report is a claim by the session, never a reading: the agent can run the
script, or post to the route, with numbers of its own. So the route files it
under the account the platform holds for the session and no other (the
token's ``sid`` must be the body's ``session_id``, and the body names no
account), the engine reads it against its own declaration
(``ExecutionLayer.parse_usage_report``), and the windows module stores it as
the hint its engine declares (``UsageProfile.in_band``): shown on the cards,
at most one confirming poll, never a move, a rest or the end of one
(``subscription_windows.record_for_session``). A session may report once per
``_MIN_INTERVAL_S``; a session on a paired computer sends the same body
through its machine's tunnel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import config
from api.sessions.sessions import verify_session_match_async

logger = logging.getLogger("otodock")

router = APIRouter()

# A status line runs after every answer and every redraw; the script posts
# only when the numbers changed, and the route takes one report per session
# in this long.
_MIN_INTERVAL_S = 5.0
_REPORT_MAX_BYTES = 2048
_SEEN_MAX = 4096
_last_report: OrderedDict[str, float] = OrderedDict()
_last_report_lock = threading.Lock()


class UsageReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)
    rate_limits: dict


def _admit(session_id: str, now: float) -> bool:
    with _last_report_lock:
        last = _last_report.get(session_id)
        if last is not None and now - last < _MIN_INTERVAL_S:
            return False
        _last_report[session_id] = now
        _last_report.move_to_end(session_id)
        while len(_last_report) > _SEEN_MAX:
            _last_report.popitem(last=False)
    return True


def _record(session_id: str, rate_limits: dict) -> bool | None:
    """The engine's reading of the report, recorded on the session's own
    account. None when the engine does not know the report's shape."""
    from core.session.session_manager import get_all_layers
    from services.engines import subscription_pool, subscription_windows
    from storage.billing import subscription_store
    sub_id = subscription_pool.get_session_subscription(session_id)
    if not sub_id or sub_id == "default":
        return False
    row = subscription_store.get_subscription(sub_id) or {}
    layer = get_all_layers().get(str(row.get("layer") or ""))
    windows = layer.parse_usage_report(rate_limits) if layer is not None else None
    if windows is None:
        return None
    return subscription_windows.record_for_session(session_id, windows)


@router.post("/v1/hooks/usage-report")
async def hook_usage_report(req: UsageReportRequest, authorization: str | None = Header(None)):
    """File one report under the session's own account. 403 for the master
    key or a token of another session, 429 for a session that reported less
    than ``_MIN_INTERVAL_S`` ago, 422 for a report the engine does not know."""
    bearer = (authorization or "").split(" ", 1)
    if len(bearer) == 2 and config.is_master_key(bearer[1]):
        raise HTTPException(status_code=403, detail="A usage report comes from a session")
    await verify_session_match_async(authorization, req.session_id)
    if len(json.dumps(req.rate_limits)) > _REPORT_MAX_BYTES:
        raise HTTPException(status_code=422, detail="This usage report is too large")
    if not _admit(req.session_id, time.monotonic()):
        raise HTTPException(status_code=429, detail="This session reported its usage a moment ago")
    recorded = await asyncio.to_thread(_record, req.session_id, req.rate_limits)
    if recorded is None:
        raise HTTPException(status_code=422, detail="This usage report is not one the engine knows")
    return {"ok": True, "recorded": recorded}
