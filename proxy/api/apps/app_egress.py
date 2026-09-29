"""The egress route (APPS.md "Secrets"): ``ANY /v1/apps/{id}/egress/{host}/
{path}`` — an app's server calls a vendor through the platform, and the
platform adds the headers its ``sends_to`` secrets declare for that host.
The value never enters the sandbox: the server holds a name, the route
holds the key.

The bearer is the running instance's launch token (basis ``app``); the
host must be one of the app's approved egress hosts; the call is always
``https://`` to a host that answers with a public address (403 with the
reason otherwise); the app's own ``Authorization``, the hop-by-hop set and every
``X-OtoDock-*`` / ``X-Forwarded-*`` header are dropped; a redirect is not
followed (502); 1 MB in, 30 s, twenty calls a second per app; the answer is
streamed back minus ``Set-Cookie`` and the hop-by-hop set. Nothing of the
exchange — the headers least of all — is logged.
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from api.apps import app_proxy
from api.apps import manifest as _mf
from auth.request_path import has_traversal
from services.apps import app_secrets
from services.infra.outbound_url import validate_outbound_url
from storage import database as task_store

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()

EGRESS_RATE = 20.0
EGRESS_TIMEOUT_S = 30.0

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """A client of its own (never the app proxy's loopback client): no
    cookie jar, no redirects, the vendor's timeout."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=EGRESS_TIMEOUT_S, follow_redirects=False)
        _client.cookies.jar = app_proxy._NoCookieJar()
    return _client


def _outbound_headers(request: Request, injected: dict[str, str]) -> list[tuple[str, str]]:
    """The app's request headers minus what the proxy owns, with the
    declared secrets' headers on top (a same-named header the app sent is
    replaced, never joined)."""
    lower_injected = {k.lower() for k in injected}
    out: list[tuple[str, str]] = []
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in app_proxy._DROP_REQUEST or lk.startswith(app_proxy._DROP_REQUEST_PREFIXES):
            continue
        if lk in lower_injected:
            continue
        out.append((k, v))
    out.extend(injected.items())
    return out


def _raw_rest(request: Request, app_id: str, path: str) -> str:
    """The path after the host as the server sent it (no decoding, no
    re-encoding), so the vendor receives exactly what was checked."""
    raw = request.scope.get("raw_path") or b""
    raw_s = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    prefix = f"/v1/apps/{app_id}/egress/"
    if raw_s.startswith(prefix):
        after_host = raw_s[len(prefix):].split("/", 1)
        return "/" + (after_host[1] if len(after_host) == 2 else "")
    return "/" + path


@router.api_route("/v1/apps/{app_id}/egress/{host}/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def app_egress(app_id: str, host: str, path: str, request: Request):
    row = await asyncio.to_thread(app_proxy._row_or_404, app_id)
    caller = await app_proxy.resolve_caller(request, row)
    if caller.basis != "app":
        raise app_proxy._refuse(403, "the app's own launch token is required")
    # The preview copy runs code nobody approved: it never spends the app's
    # keys (APPS.md "Secrets").
    if caller.instance != "live":
        raise app_proxy._refuse(403, "the preview copy cannot call vendors with the app's keys: "
                                     "deploy the app to use its egress")
    # The deploy writes a changed manifest onto the row before anyone
    # approves it, so while one waits on the card the hosts and the
    # `sends_to` targets here are the unapproved ones: nothing goes out.
    if not task_store.app_actions_approved(row):
        raise app_proxy._refuse(403, "the app's manifest is waiting for approval")
    host = (host or "").strip().lower().rstrip(".")
    if not host or host not in _mf.parse_egress(row):
        raise app_proxy._refuse(404, "not an egress host of this app")
    rest = _raw_rest(request, app_id, path)
    if has_traversal(path) or has_traversal(rest):
        raise app_proxy._refuse(404, "Not found")
    if not app_proxy._take(f"{app_id}|egress", EGRESS_RATE, EGRESS_RATE):
        raise app_proxy._refuse_with_retry(429, "too many outbound calls", 1)
    # The request is made by the proxy process on the platform's network: an
    # approved host must answer with a public address, every address it
    # resolves to (a LAN name behind a public label is refused, with the
    # address in the reason).
    reason = await asyncio.to_thread(validate_outbound_url, f"https://{host}/", require_https=True)
    if reason:
        raise app_proxy._refuse(403, f"egress refused: {reason}")
    body = await app_proxy._read_body(request)
    injected = await asyncio.to_thread(app_secrets.outbound_headers, row, host)
    headers = _outbound_headers(request, injected)
    query = request.scope.get("query_string") or b""
    url = f"https://{host}{rest}"
    if query:
        url += "?" + (query.decode("latin-1") if isinstance(query, bytes) else str(query))
    client = _http()
    try:
        upstream = await client.send(
            client.build_request(request.method, url, headers=headers, content=body),
            stream=True)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        logger.warning("App %s: egress to %s failed: %s", row.get("slug"), host,
                       type(e).__name__)
        raise app_proxy._refuse(502, "the host did not answer")
    if 300 <= upstream.status_code < 400 and upstream.status_code != 304:
        await upstream.aclose()
        raise app_proxy._refuse(502, "the host answered with a redirect")
    out_headers = [
        (k, v) for k, v in upstream.headers.multi_items()
        if k.lower() not in app_proxy._DROP_RESPONSE and not k.lower().startswith("access-control-")
    ]
    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=dict(out_headers))
    return StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code,
        headers=dict(out_headers), background=BackgroundTask(upstream.aclose),
    )
