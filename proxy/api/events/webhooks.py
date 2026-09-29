"""Public webhook receive endpoint.

Path: ``/v1/webhooks/{provider_id}/{subscription_id}``

UNAUTHENTICATED at the HTTP layer — auth is provided by the vendor's
signature in the request itself (HMAC-SHA256 typically). The dispatcher
runs the manifest's signature verification against the row's signing
secret before doing anything else.

GET is supported only so MS Graph's ``?validationToken=xyz`` handshake
works (their first call is a GET when creating subscriptions); other
vendors POST.

**Reverse-proxy bypass required**: this path must be excluded from any
OIDC/forward-auth gate (Authentik, Authelia, oauth2-proxy, Cloudflare
Access) the platform sits behind. Same requirement as
``/v1/triggers/{scope}/{owner}/{slug}``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, Response
from starlette.requests import ClientDisconnect

import config
from auth import rate_limiter
from auth.lan_check import client_address
from services.webhooks import webhook_dispatcher

logger = logging.getLogger("claude-proxy.api.webhooks")
router = APIRouter()

# The unauthenticated body read: capped at MAX_WEBHOOK_BODY_BYTES (the
# HTTP middleware's body cap is the outer bound), a gap limit
# between two chunks and a limit for the whole body, and a bound on bodies
# read at once, per client address and in all.
_CHUNK_GAP_S = 10.0
_BODY_S = 30.0
_READS_PER_CLIENT = 4
_READS_TOTAL = 256
_reads = {"total": 0}
_reads_by_client: dict[str, int] = {}
# The refusals counted against the client address in ``webhook_receive_ip``:
# the ones a request earns before any subscription is found. A 404 or 410 (a
# deleted or disabled subscription a vendor keeps posting to) never counts,
# nor does a delivered event: the relay forwards every vendor from one address.
_COUNTED_REFUSALS = frozenset({400, 401, 408, 413})


class _BodyTooLarge(Exception):
    pass


def _refusal(status: int, error: str, *, retry_after: int | None = None) -> Response:
    headers = {"Connection": "close"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return Response(content=json.dumps({"error": error}).encode(), status_code=status,
                    media_type="application/json", headers=headers)


async def _read_body(request: Request) -> bytes:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _BODY_S
    cap = config.MAX_WEBHOOK_BODY_BYTES
    chunks: list[bytes] = []
    total = 0
    stream = request.stream().__aiter__()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        try:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=min(_CHUNK_GAP_S, remaining))
        except StopAsyncIteration:
            break
        total += len(chunk)
        if cap and total > cap:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _release_read(key: str) -> None:
    _reads["total"] -= 1
    left = _reads_by_client.get(key, 1) - 1
    if left > 0:
        _reads_by_client[key] = left
    else:
        _reads_by_client.pop(key, None)


async def _receive(request: Request, dispatch) -> Response:
    """The receive steps every vendor and relay POST takes before the
    dispatcher: the per-address throttle (a distinct client only: behind an
    edge whose address every client shares, one bucket would let one sender
    block every vendor), the read bounds, the capped read; the dispatcher's
    answer passes through unchanged. The read bounds count bodies being
    read: the slot is given back when the read ends, before the dispatch,
    which the dispatcher's own pre-auth gate bounds."""
    addr = client_address(request)
    key = f"ip:{addr.client}"
    if not addr.shared:
        ok, retry_after = rate_limiter.check_rate_limit("webhook_receive_ip", key)
        if not ok:
            return _refusal(429, "too_many_requests", retry_after=retry_after)
    if (_reads["total"] >= _READS_TOTAL
            or (not addr.shared and _reads_by_client.get(key, 0) >= _READS_PER_CLIENT)):
        return _refusal(503, "busy", retry_after=5)
    _reads["total"] += 1
    _reads_by_client[key] = _reads_by_client.get(key, 0) + 1
    try:
        raw_body = await _read_body(request)
    except (_BodyTooLarge, ClientDisconnect):
        status, response = 413, _refusal(413, "body_too_large")
    except TimeoutError:
        status, response = 408, _refusal(408, "body_timeout")
    else:
        status = 0
    finally:
        _release_read(key)
    if not status:
        status, body, headers = await dispatch(raw_body)
        response = Response(
            content=body if isinstance(body, bytes) else (
                json.dumps(body) if isinstance(body, (dict, list)) else str(body)).encode("utf-8"),
            status_code=status, headers=headers or {},
        )
    if status in _COUNTED_REFUSALS and not addr.shared:
        rate_limiter.record_attempt("webhook_receive_ip", key)
    return response


@router.post(
    "/v1/webhooks/relay/{provider_id}",
    include_in_schema=False,
)
async def receive_relay_webhook(provider_id: str, request: Request) -> Response:
    """Receive a relay-FORWARDED vendor event (hosted event delivery).

    Declared ABOVE the generic vendor route — both have two path segments
    and Starlette matches in declaration order ('relay' is a reserved
    provider_id in the manifest validator). POST-only: the relay answers
    vendor handshakes (url_verification etc.) upstream. Auth = the relay's
    forward signature over the verbatim body (X-OtoDock-Event-* headers);
    same reverse-proxy forward-auth bypass requirement as the vendor route.
    """
    headers = {k.lower(): v for k, v in request.headers.items()}

    async def dispatch(raw_body: bytes):
        try:
            return await webhook_dispatcher.dispatch_relay_webhook(
                provider_id=provider_id,
                raw_body=raw_body,
                headers=headers,
            )
        except Exception:
            logger.exception(
                "relay webhook dispatcher raised unexpectedly for provider=%s",
                provider_id,
            )
            return 500, {"error": "internal_error"}, {"content-type": "application/json"}

    return await _receive(request, dispatch)


@router.api_route(
    "/v1/webhooks/{provider_id}/{subscription_id}",
    methods=["GET", "POST"],
    include_in_schema=False,  # vendor URLs aren't documented for human consumption
)
async def receive_webhook(
    provider_id: str,
    subscription_id: str,
    request: Request,
) -> Response:
    """Receive a webhook from a vendor.

    Returns the dispatcher's response shape:
      * 200 + JSON ``{status, fired, ...}`` on a normal event (even if
        no triggers matched — we want vendors to NOT retry)
      * 200 + handshake-specific body on URL-verification handshakes
      * 401 on signature mismatch
      * 404 when subscription_id is unknown
      * 410 when subscription is disabled / failed
    """
    # Lowercase + simple-string headers for the dispatcher.
    headers = {k.lower(): v for k, v in request.headers.items()}
    query_params = dict(request.query_params)

    async def dispatch(raw_body: bytes):
        try:
            return await webhook_dispatcher.dispatch_webhook(
                provider_id=provider_id,
                subscription_id=subscription_id,
                raw_body=raw_body,
                headers=headers,
                query_params=query_params,
                http_method=request.method,
            )
        except Exception:
            logger.exception(
                "webhook dispatcher raised unexpectedly for provider=%s sub=%s",
                provider_id, subscription_id,
            )
            return 500, {"error": "internal_error"}, {"content-type": "application/json"}

    return await _receive(request, dispatch)
