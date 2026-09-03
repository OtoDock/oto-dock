"""Phone usage reporting — local cost-tracking for the phone daemon.

The phone daemon's turn classifier (Groq) makes non-streaming calls whose token
spend is otherwise invisible in /admin/usage. The daemon drains the accumulated
usage at each call's teardown and POSTs it here; we record ONE usage_records row
per call (``source_type='turn-classifier'``, ``scope='agent'``, ``user_sub='phone'``)
so it rolls up in the per-agent breakdown — mirroring how phone turns + title
generation already attribute.

Auth = the internal proxy master key ONLY (the same Bearer the daemon uses
for warmup) — session JWTs are refused, see ``_require_master_key``. The hosted relay bills the classifier independently (×1.25
credit); this records the BASE price locally for display — a separate ledger.
"""

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import config
from core.layers.providers import ProviderUsage, get_adapter
from services.billing import usage_service
from storage import phone_call_log_store, phone_route_store

logger = logging.getLogger("claude-proxy")
router = APIRouter()


def _require_master_key(authorization: str | None) -> None:
    """The phone daemon is the ONLY legitimate writer here. The generic
    ``verify_api_key`` also admits session-scoped JWTs — which every
    sandboxed agent subprocess holds — and those must not be able to forge
    call-log rows or pollute usage records."""
    token = ""
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
    if not config.is_master_key(token):
        raise HTTPException(status_code=401, detail="Master API key required")



# The turn classifier is fixed to this Groq model (dispatcher builds it with no model
# arg). Used to price the row when the daemon doesn't report a model.
_DEFAULT_MODEL = "openai/gpt-oss-120b"


class TurnClassifierUsage(BaseModel):
    agent: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str = ""


def _record(agent: str, model: str, in_tok: int, out_tok: int, session_id: str | None) -> dict:
    """Price + persist the row (sync DB work — run off the event loop)."""
    cost = get_adapter("groq").calculate_cost(
        model, ProviderUsage(input_tokens=in_tok, output_tokens=out_tok))
    row_id = usage_service.record_usage(
        user_sub="phone",
        agent=agent,
        scope="agent",
        source_type="turn-classifier",
        source_id=session_id,
        cost_usd=cost,
        input_tokens=in_tok,
        output_tokens=out_tok,
        message_count=0,
        provider="groq",
        model=model,
    )
    return {"recorded": bool(row_id), "cost_usd": cost}


@router.post("/v1/phone/usage/turn-classifier")
async def report_turn_classifier_usage(
    req: TurnClassifierUsage, authorization: str | None = Header(None),
):
    """Record one phone call's turn-classifier token spend (local per-agent display)."""
    _require_master_key(authorization)
    model = req.model or _DEFAULT_MODEL
    in_tok = max(0, int(req.input_tokens))
    out_tok = max(0, int(req.output_tokens))
    if not (in_tok or out_tok):
        return {"recorded": False}
    return await asyncio.to_thread(
        _record, req.agent or "", model, in_tok, out_tok, req.session_id or None)


# ---------------------------------------------------------------------------
# Call-outcome reporting (admin call log)
# ---------------------------------------------------------------------------

class CallReport(BaseModel):
    """One call's teardown report from the daemon — fields it may not know
    (route_name, outbound caller-ID) are enriched here from the route row."""
    route_id: str = ""
    phone_server_id: int | None = None
    agent: str = ""
    direction: str = "inbound"
    from_number: str = ""
    to_number: str = ""
    transport: str = ""
    call_uuid: str = ""
    outcome: str = "failed"
    pin_attempts: int = 0
    started_at: str = ""
    ended_at: str = ""
    duration_s: int | None = None


def _record_call(data: dict) -> dict:
    """Validate/enrich + insert one phone_call_log row (sync DB work)."""
    if data.get("outcome") not in phone_call_log_store.VALID_OUTCOMES:
        data["outcome"] = "failed"
    if data.get("direction") not in ("inbound", "outbound"):
        data["direction"] = "inbound"
    data["pin_attempts"] = max(0, int(data.get("pin_attempts") or 0))
    route = (phone_route_store.get_route(data["route_id"])
             if data.get("route_id") else None)
    if route:
        data["route_name"] = route.get("name", "")
        data["agent"] = data.get("agent") or route.get("agent", "")
        data["phone_server_id"] = (data.get("phone_server_id")
                                   or route.get("phone_server_id"))
        if data["direction"] == "outbound" and not data.get("from_number"):
            data["from_number"] = route.get("ami_caller_id", "") or ""
    else:
        # Unknown/deleted route: keep the row, drop the FK.
        data["route_id"] = ""
    row_id = phone_call_log_store.insert_call(data)
    return {"recorded": True, "id": row_id}


@router.post("/v1/phone/calls/report")
async def report_phone_call(
    req: CallReport, authorization: str | None = Header(None),
):
    """One row per call for the admin call log — fired at daemon teardown,
    including calls that never warmed a session (PIN-refused, capacity-
    rejected). Entered digits never travel here — only attempt counts."""
    _require_master_key(authorization)
    return await asyncio.to_thread(_record_call, req.model_dump())
