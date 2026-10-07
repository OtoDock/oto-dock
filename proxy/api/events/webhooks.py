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

import json
import logging

from fastapi import APIRouter, Request, Response
from starlette.requests import ClientDisconnect

from api.events import webhook_body
from auth import rate_limiter, webhook_providers
from auth.lan_check import client_address
from services.webhooks import webhook_dispatcher

logger = logging.getLogger("claude-proxy.api.webhooks")
router = APIRouter()

# The refusals counted against the client address in ``webhook_receive_ip``:
# the ones a request earns before any subscription is found. A 404 or 410 (a
# deleted or disabled subscription a vendor keeps posting to) never counts,
# nor does a delivered event: the relay forwards every vendor from one address.
_COUNTED_REFUSALS = frozenset({400, 401, 408, 413})


def _refusal(status: int, error: str, *, retry_after: int | None = None) -> Response:
    headers = {"Connection": "close"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return Response(content=json.dumps({"error": error}).encode(), status_code=status,
                    media_type="application/json", headers=headers)


def _response(status: int, body, headers) -> Response:
    return Response(
        content=body if isinstance(body, bytes) else (
            json.dumps(body) if isinstance(body, (dict, list)) else str(body)).encode("utf-8"),
        status_code=status, headers=headers or {},
    )


# An early answer reads a body this small first, so its sender sees the
# status rather than a reset connection; a larger one is left unread and
# the answer closes the connection (the HTTP middleware).
_DRAIN_MAX = 64 * 1024


async def _drain_small(request: Request, source: str) -> None:
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) <= _DRAIN_MAX:
            await webhook_body.read(request, cap=_DRAIN_MAX, source=source)
    except Exception:
        pass


async def _receive(request: Request, dispatch, *, source: str, prepare=None) -> Response:
    """The receive steps every vendor and relay POST takes before the
    dispatcher: the per-address throttle (a distinct client only: behind an
    edge whose address every client shares, one bucket would let one sender
    block every vendor), ``prepare`` (run before the body is read: an early
    answer, or the cap the sender may use and what to do when it is
    exceeded), the capped read (``webhook_body``: its deadlines and the
    in-flight bounds); the dispatcher's answer passes through unchanged."""
    addr = client_address(request)
    key = f"ip:{addr.bucket_key}"
    if not addr.shared:
        ok, retry_after = rate_limiter.check_rate_limit("webhook_receive_ip", key)
        if not ok:
            return _refusal(429, "too_many_requests", retry_after=retry_after)
    cap, on_too_large = webhook_body.unknown_cap(), None
    if prepare is not None:
        early, cap, on_too_large = await prepare()
        if early is not None:
            await _drain_small(request, source)
            return _response(*early)
    if cap > webhook_body.unknown_cap():
        webhook_body.lift(request, cap)
    try:
        raw_body = await webhook_body.read(request, cap=cap, source=source)
    except webhook_body.TooLarge:
        status, response = 413, _refusal(413, "body_too_large")
        if on_too_large is not None:
            await on_too_large(cap)
    except ClientDisconnect:
        # The sender went away, or the middleware's cap cut the read.
        status, response = 413, _refusal(413, "body_too_large")
        if on_too_large is not None and request.scope.get("otodock.body_cut"):
            await on_too_large(cap)
    except webhook_body.Busy:
        status, response = 503, _refusal(503, "busy", retry_after=5)
    except TimeoutError:
        status, response = 408, _refusal(408, "body_timeout")
    else:
        status, body, headers = await dispatch(raw_body)
        response = _response(status, body, headers)
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

    return await _receive(request, dispatch, source=f"relay/{provider_id}")


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
                context=loaded.get("context"),
            )
        except Exception:
            logger.exception(
                "webhook dispatcher raised unexpectedly for provider=%s sub=%s",
                provider_id, subscription_id,
            )
            return 500, {"error": "internal_error"}, {"content-type": "application/json"}

    loaded: dict = {}

    async def prepare():
        context, early = await webhook_dispatcher.load_receive_context(
            provider_id, subscription_id)
        if early is not None:
            return early, 0, None
        loaded["context"] = context
        cap = webhook_body.unknown_cap()
        # A signature is over the body: the larger read is allowed for a
        # provider whose events run large and whose manifest declares one,
        # and the dispatcher verifies it before anything else reads the body.
        if (provider_id in webhook_providers.LARGE_BODY_PROVIDERS
                and context.webhooks_block.get("signature")):
            cap = webhook_body.signed_cap()

        async def too_large(c: int) -> None:
            await webhook_dispatcher.note_body_refusal(context.row, c)
        return None, cap, too_large

    return await _receive(request, dispatch, source=f"{provider_id}/{subscription_id}",
                          prepare=prepare)
