"""A tool's saved answers and its start, asked by its stand-in
(``core/stdio_path_interceptor.py``) on the tool's fetch token: ``GET
/v1/hooks/tool-listing`` answers what the tool's program said in an earlier
session of the same agent and person, and ``POST /v1/hooks/tool-start``
judges the start of its program (``start``) and keeps what it answered
(``started``), and gives back the room of one stopped after its answers were
kept (``stopped``). The token is checked as the secrets route checks it
(``mcp_broker.live_token``): valid, and its session still holds the tool's
bundle. Internal listener only (``middleware._SESSION_CREDENTIAL_ROUTES``).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger("otodock")

router = APIRouter()

# One kept answer per session and tool: a session's later calls with its token
# keep nothing more (the next real start is the next session's).
_KEPT_MAX = 4096
_kept: OrderedDict[tuple[str, str], None] = OrderedDict()


def _live(authorization: str | None) -> tuple[str, str]:
    from api.hooks.credentials import capability_token
    from core.credentials import mcp_broker
    live = mcp_broker.live_token(capability_token(authorization))
    if isinstance(live, int):
        raise HTTPException(status_code=live, detail="This tool's session is not running")
    return live[0], live[1]


def _scope(session_id: str) -> tuple[str, str] | None:
    from core.session.session_state import get_session_security
    from services.mcp import tool_listing
    ctx = get_session_security(session_id)
    return tool_listing.scope_of(ctx) if ctx is not None else None


@router.get("/v1/hooks/tool-listing")
async def hook_tool_listing(fp: str = Query(..., min_length=16, max_length=128),
                            authorization: str | None = Header(None)):
    """The answers saved for this tool and fingerprint in the session's own
    scope, or 404."""
    from services.mcp import tool_listing
    sid, server = _live(authorization)
    scope = _scope(sid)
    listing = await asyncio.to_thread(tool_listing.saved, scope, server, fp) if scope else None
    if listing is None:
        raise HTTPException(status_code=404, detail="Nothing saved for this tool")
    return {"listing": listing}


class ToolStartRequest(BaseModel):
    phase: str = Field(pattern="^(start|started|failed|listed|stopped)$")
    fp: str = Field(default="", max_length=128)
    with_session: bool = False
    ms: int = Field(default=0, ge=0)
    listing: dict | None = None


@router.post("/v1/hooks/tool-start")
async def hook_tool_start(req: ToolStartRequest, authorization: str | None = Header(None)):
    """``listed``: the engine has the stand-in's tool list (from saved
    answers). ``start``: whether the program may start now. A late start is judged
    by the session's box (``concurrency.grow``); a start with the session is
    counted. ``started``: one line for the measurement, and the program's
    answers kept for the next session when there were none or they changed;
    ``kept`` says whether they were (a program started only for them then
    stops). ``failed``: a start that ``start`` counted did not happen, its
    program given back. ``stopped``: a program started for its answers
    stopped once they were kept and nothing called it, given back."""
    from core import concurrency
    from services.mcp import tool_listing
    sid, server = _live(authorization)
    if req.phase in ("listed", "failed") or (req.phase == "started" and req.with_session):
        # The engine has the server's tool list, or gets none (its program
        # did not start): a terminal's first message waits for it no longer
        # (``tool_listing.pending_listed``).
        tool_listing.note_listed(sid, server)
        if req.phase == "listed":
            return {"ok": True}
    if req.phase == "failed":
        logger.info("tool %s of %s did not start", server, sid[:8])
        await concurrency.shrink(sid, server)
        return {"ok": True}
    if req.phase == "stopped":
        logger.info("tool %s of %s stopped: its answers are kept and nothing called it", server, sid[:8])
        await concurrency.shrink(sid, server)
        return {"ok": True}
    if req.phase == "start":
        admission = await concurrency.grow(sid, judged=not req.with_session, server=server)
        if not admission:
            logger.info("tool %s of %s not started: %s", server, sid[:8], admission.reason)
            return {"ok": False, "message": admission.user_message}
        return {"ok": True}
    logger.info("tool %s of %s started %s in %d ms", server, sid[:8],
                "with its session" if req.with_session else "on its first call", req.ms)
    kept = False
    if req.listing is not None and req.fp:
        key = (sid, server)
        scope = _scope(sid)
        if scope is not None and key not in _kept:
            _kept[key] = None
            while len(_kept) > _KEPT_MAX:
                _kept.popitem(last=False)
            kept = await asyncio.to_thread(tool_listing.keep, scope, server, req.fp, req.listing)
    return {"ok": True, "kept": bool(kept)}
