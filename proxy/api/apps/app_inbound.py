"""The inbound route (APPS.md "Inbound hooks"): ``POST /v1/apps/{id}/
inbound/{name}`` — no authentication of its own, the one unauthenticated
write path into the platform besides the generic webhook, and a
verified-then-enqueue path with no other side effect. In this order: the
id's shape and the row (a malformed or unknown app, or a personal app whose
owner left its agent, is the one 404 and never counts against a bucket), the
per-address bucket (a probe of hook names on a real app counts; skipped for
an address every client shares, as the webhook receivers skip theirs: one
bucket for every vendor behind an edge would let any sender refuse them
all), the per-app bucket, the hook in the row's signed block, the manifest
approved and the secret set (else one 404 for all three: nothing about the
app's state leaks), the
raw body under 48 KB (413), the failed-signature bucket of the hook (429;
per client for a distinct client), the scheme over the raw bytes (401 on a bad
signature — an operator debugging a pasted secret needs it), the wrapped
payload sized (413), then ``enqueue`` for the named handler with basis
``inbound`` and the vendor's event id — a replay answers ``duplicate``,
a full queue 503 with ``Retry-After`` so the vendor retries. The route
wakes nothing itself: the drain does, as for every wake.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.apps import manifest as _mf
from auth import rate_limiter
from auth.lan_check import client_address
from services.apps import app_handlers, app_inbound
from storage import database as task_store
from storage import db_app_deliveries as deliveries
from storage import db_app_secrets
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail="too many requests",
                         headers={"Retry-After": str(max(1, retry_after))})


async def _read_body(request: Request) -> bytes:
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > app_inbound.BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="the body is larger than 48 KB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/apps/{app_id}/inbound/{name}")
async def inbound(app_id: str, name: str, request: Request):
    from api.apps.app_proxy import _APP_ID_RE
    if not _APP_ID_RE.match(app_id or ""):
        raise _not_found()
    row = await run_db(task_store.get_app, app_id)
    if not row or row.get("hidden") or not task_store.app_kind_of(row).may_serve \
            or await run_db(task_store.personal_row_dormant, row):
        raise _not_found()
    addr = client_address(request)
    if not addr.shared:
        ok, retry_after = rate_limiter.hit("app_inbound_ip", f"ip:{addr.bucket_key}")
        if not ok:
            raise _too_many(retry_after)
    ok, retry_after = rate_limiter.hit("app_inbound_app", row["id"])
    if not ok:
        raise _too_many(retry_after)
    spec = _mf.parse_inbound(row).get(name)
    if not spec or not task_store.app_actions_approved(row):
        raise _not_found()
    secret = (await asyncio.to_thread(db_app_secrets.values, row["id"], [spec["secret"]])).get(spec["secret"])
    if not secret:
        raise _not_found()
    raw = await _read_body(request)
    # Failed signatures per hook, and per client for a distinct client, so a
    # sender on its own address locks only itself and one hook's stale
    # secret never locks another hook. No await from the check to the
    # record: a burst of wrong signatures cannot all pass the check first.
    fail_key = f"{row['id']}:{name}" if addr.shared else f"{row['id']}:{name}:ip:{addr.bucket_key}"
    ok, retry_after = rate_limiter.check_rate_limit("app_inbound_fail", fail_key)
    if not ok:
        logger.info("App %s: inbound %s refused (too many failed signatures)", row.get("slug"), name)
        raise _too_many(retry_after)
    verdict = app_inbound.verify(spec, raw, request.headers, secret)
    if not verdict.ok:
        if verdict.reason == "signature_mismatch":
            rate_limiter.record_attempt("app_inbound_fail", fail_key)
        logger.info("App %s: inbound %s refused (%s)", row.get("slug"), name, verdict.reason)
        raise HTTPException(status_code=401,
                            detail=f"signature verification failed ({verdict.reason})")
    event_id = app_inbound.event_id_for(name, verdict.event_id)
    payload = app_inbound.payload_for(name, spec, raw, request.headers, verdict.event_id)
    if not app_inbound.wrapped_size_ok(payload):
        raise HTTPException(status_code=413, detail="the event is larger than the delivery cap")
    d = await app_handlers.enqueue(row, spec["handler"], f"inbound:{name}", payload,
                                   event_id=event_id)
    if d is None:
        first = await run_db(deliveries.find_event, row["id"], spec["handler"], event_id)
        return {"status": "ok", "duplicate": True, "delivery_id": (first or {}).get("id", "")}
    if d.get("status") == deliveries.DEAD:
        return JSONResponse({"detail": d.get("last_error") or "not accepted right now"},
                            status_code=503, headers={"Retry-After": "60"})
    return {"status": "ok", "delivery_id": d["id"]}
