"""External links (SHARING.md): ``/s/{token}`` and what a link may do.

A link is a share row whose token is known only to whoever holds the URL
(the row keeps its sha256). The host page here is plain HTML on the
platform origin that loads the same sandboxed-iframe host code the
dashboard uses (``/ui-kit/share-host.js``); it never reads the dashboard
``session`` cookie. Password mode sets one HttpOnly cookie per share,
bound to the share id and to the current password hash, scoped to the
link's own path. Unknown, expired and revoked links all render the one
404 page. Actions run only when the link's Buttons switch is on, only
``mcp_tool`` and ``fire_task``, on the app's identity, attributed to the
share, under a per-share daily cap; feeds, navigation and prompts are
never available to a link.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect
from pydantic import BaseModel

import config
from api.apps import manifest as _mf
from api.apps.app_actions import _check_fire_rate, _run_action
from api.apps.apps import APP_RUNTIME, _app_document
from api.media.ui import _placeholder, _ui_response, inject_runtime, is_full_document, request_origin, wrap_fragment
from auth import rate_limiter
from auth.lan_check import get_client_ip
from auth import confirm as _confirm
from auth.password import HashBusy, verify_password_async
from storage import database as task_store
from storage.sharing import share_store
from storage import db_apps

logger = logging.getLogger("claude-proxy.shares")
router = APIRouter()

UNLOCK_TTL_S = 24 * 3600
ACTIONS_PER_DAY = 200
COOKIE_PREFIX = "share_"
# Accounts kept by the host (APPS.md "External links"): the app's own
# session token in a cookie of the link's, an HS256 envelope bound to the
# share, whose lifetime the manifest sets (`external.session_days`).
SESSION_COOKIE_PREFIX = "share_session_"
SESSION_TOKEN_MAX_BYTES = 512


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load_live_link(token: str) -> tuple[dict, dict] | None:
    """The share and its target row when the link may serve: the row is
    live, the target exists (an app not soft-unpinned; a chat still there),
    and whoever made the link may still share it (a demoted or removed
    creator kills their links). Sync."""
    if not token or len(token) > 128:
        return None
    share = share_store.get_share_by_token_hash(token_hash(token))
    if not share or not share_store.is_live(share):
        return None
    # The platform switches stop existing links too, not only new ones.
    if task_store.get_platform_setting("sharing_external_enabled") == "0":
        return None
    if not share.get("password_hash") and task_store.get_platform_setting("sharing_public_links_enabled") == "0":
        return None
    if share.get("target_kind") == "chat":
        from api.agents.chats import sub_can_share_chat
        chat = task_store.get_chat(share.get("chat_id") or "")
        if not chat or not sub_can_share_chat(share.get("created_by") or "", chat):
            return None
        return share, chat
    if share.get("target_kind") != "app":
        return None
    row = task_store.get_app(share.get("app_id") or "")
    if not row or row.get("hidden") or task_store.app_is_scoped(row):
        return None
    if not _mf.sub_can_approve_surface(share.get("created_by") or "", row):
        return None
    return share, row


def _cookie_name(share: dict) -> str:
    return COOKIE_PREFIX + share["id"]


def _password_tag(share: dict) -> str:
    """What the cookie binds to: a digest of the password hash in force, so a
    password edit invalidates every cookie (the hash's own prefix is the
    bcrypt header and salt, near-constant across hashes)."""
    return hashlib.sha256((share.get("password_hash") or "").encode("utf-8")).hexdigest()[:16]


def _mint_unlock(share: dict) -> str:
    payload = {
        "purpose": "share_unlock",
        "share_id": share["id"],
        "ph": _password_tag(share),
        "iat": int(time.time()),
        "exp": int(time.time()) + UNLOCK_TTL_S,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def needs_password(share: dict) -> bool:
    return not share.get("public") and bool(share.get("password_hash"))


def is_unlocked(request: Request, share: dict) -> bool:
    """Public links need nothing; a password link needs its own cookie,
    bound to this share and to the password hash in force (a password
    edit logs everyone out)."""
    if not needs_password(share):
        return True
    raw = request.cookies.get(_cookie_name(share))
    if not raw:
        return False
    try:
        payload = jwt.decode(raw, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return (payload.get("purpose") == "share_unlock"
            and payload.get("share_id") == share["id"]
            and payload.get("ph") == _password_tag(share))


def _session_cookie_name(share: dict) -> str:
    return SESSION_COOKIE_PREFIX + share["id"]


def _mint_session(share: dict, app_token: str, days: int) -> str:
    now = int(time.time())
    return jwt.encode({"purpose": "app_session", "share_id": share["id"], "s": app_token,
                       "iat": now, "exp": now + int(days) * 86400},
                      config.JWT_SECRET, algorithm="HS256")


def session_of(request: Request, share: dict) -> str:
    """The app's own session token the link's cookie holds for this share,
    or "" — the envelope must be ours, unexpired and bound to the share."""
    raw = request.cookies.get(_session_cookie_name(share))
    if not raw:
        return ""
    try:
        payload = jwt.decode(raw, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return ""
    if payload.get("purpose") != "app_session" or payload.get("share_id") != share["id"]:
        return ""
    s = payload.get("s")
    return s if isinstance(s, str) and 0 < len(s) <= SESSION_TOKEN_MAX_BYTES else ""


def _external_claim(share: dict, row: dict, session: str) -> dict:
    """The link's viewer claim (APPS.md "External links"): external, this
    share, and — when the link's cookie holds one — the app's own session
    token inside it, which the app reads from the verified header."""
    from services.apps import app_tokens
    claims = {"principal": "external", "sub": "", "username": "", "role": "viewer",
              "grant": share["id"], "agent": row.get("agent") or "",
              "visibility": db_apps.app_scope(row.get("username")), "external": True}
    if session:
        claims["session"] = session
    tok = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, claims, app_tokens.VIEWER_TTL_S)
    return {"token": tok, "exp": int(time.time()) + app_tokens.VIEWER_TTL_S,
            "ttl": app_tokens.VIEWER_TTL_S, "session": session or None}


def _session_token_ok(value) -> bool:
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= SESSION_TOKEN_MAX_BYTES
            and all(32 <= ord(c) <= 126 for c in value))


async def _require_challenge(request: Request, row: dict) -> None:
    """A non-GET call from a link to a path the manifest marks for a
    challenge (APPS.md "External links"): while Turnstile is configured the
    call must carry a passed token as ``X-OtoDock-Challenge`` — verified
    here, once, and never forwarded to the app. Not configured → nothing
    is asked; siteverify unreachable → allowed, as the login does."""
    from api.apps import app_proxy
    from services.infra import turnstile
    settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    tcfg = turnstile.load_config(settings)
    if not tcfg.enabled:
        return
    token = (request.headers.get("x-otodock-challenge") or "").strip()
    if not token:
        raise app_proxy._refuse(403, "challenge required")
    ip = get_client_ip(request) if config.TRUSTED_PROXIES else None
    if not await turnstile.verify_token(tcfg, token, ip):
        raise app_proxy._refuse(403, "challenge failed")


def _same_origin(request: Request) -> bool:
    """A browser fetch from the link's own page: ``Sec-Fetch-Site:
    same-origin`` or an ``Origin`` equal to ours. A third-party page can
    form-post a public link's routes otherwise."""
    if request.headers.get("sec-fetch-site", "").lower() == "same-origin":
        return True
    origin = request.headers.get("origin", "")
    return bool(origin) and origin.rstrip("/") == request_origin(request).rstrip("/")


def _json_only(request: Request) -> None:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json" or not _same_origin(request):
        raise HTTPException(status_code=403, detail="not available")


_NOT_AVAILABLE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Not available</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;font:15px system-ui,sans-serif;background:#f6f7f9;color:#334}
@media (prefers-color-scheme:dark){body{background:#111318;color:#cdd3dc}}
p{max-width:28rem;text-align:center;padding:0 1.5rem}</style></head>
<body><p>This link is not available. It may have expired or been withdrawn.</p></body></html>"""


def _not_available_page() -> HTMLResponse:
    return HTMLResponse(_NOT_AVAILABLE_PAGE, status_code=404,
                        headers={"Cache-Control": "no-store"})


def _release_sha(share: dict, row: dict) -> str:
    return "" if share.get("target_kind") == "chat" else (row.get("release_sha256") or "")


# The link page's own policy. Every frame it holds is served under
# ``/s/<token>/`` (the app, a snapshot's artifacts), so ``frame-src`` pins
# them to this origin: a sandboxed artifact that navigates its own frame
# elsewhere would otherwise carry its URL, the link token, to that site.
# Turnstile is the one outside script and frame.
_HOST_PAGE_CSP = (
    "frame-ancestors 'none'; "
    "frame-src 'self' https://challenges.cloudflare.com; "
    "script-src 'self' https://challenges.cloudflare.com; "
    "object-src 'none'; base-uri 'none'"
)


def _host_page(share: dict, row: dict, unlocked: bool, site_key: str) -> str:
    """The page the link serves. The token is not in it: the host reads it
    from its own address (``/s/<token>``), so nothing request-derived is
    written into the HTML."""
    is_chat = share.get("target_kind") == "chat"
    title = row.get("title") or row.get("slug") or ("Shared conversation" if is_chat else "Shared app")
    # What the link may do (APPS.md "External links"): the hosts the page
    # may open — once the manifest is approved — and the session's lifetime.
    ext = _mf.parse_external(row) if not is_chat else {"links": [], "session_days": 30}
    approved = (not is_chat) and task_store.app_actions_approved(row)
    cfg = {
        "share_id": share["id"],
        "kind": "chat" if is_chat else "app",
        "title": title,
        "needs_password": needs_password(share) and not unlocked,
        "actions": bool(share.get("allow_actions")) and not is_chat,
        "turnstile_site_key": site_key,
        # A folder app's document is addressed by its release hash (APPS.md
        # "Client files"); the host builds the frame's src from it.
        "folder": (not is_chat) and db_apps.app_kind_of(row).serves_tree,
        # The hash addresses the release's files, which the frame fetches
        # with no cookie: withheld until the password step is done (the
        # unlock answer carries it).
        "release_sha": _release_sha(share, row) if unlocked else "",
        "external_links": list(ext["links"]) if approved else [],
        "session_days": int(ext["session_days"]),
    }
    # The config rides a JSON script block: no `<` may reach the parser
    # (`</script>` ends the block, `<!--` puts it in escape mode).
    cfg_json = json.dumps(cfg).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{html.escape(title)}</title>
<style>html,body{{margin:0;height:100%;font:15px system-ui,sans-serif;background:#f6f7f9;color:#223}}
@media (prefers-color-scheme:dark){{html,body{{background:#111318;color:#dde3ea}}}}
#root{{height:100%;display:flex;flex-direction:column}}</style></head>
<body><div id="root"></div>
<script type="application/json" id="share-config">{cfg_json}</script>
<script src="/ui-kit/share-host.js"></script>
</body></html>"""


@router.get("/s/{token}")
async def share_page(token: str, request: Request):
    """The link's page: the password form, or the app in its sandbox."""
    found = await asyncio.to_thread(load_live_link, token)
    if not found:
        return _not_available_page()
    share, row = found
    from services.infra import turnstile
    settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    tcfg = turnstile.load_config(settings)
    page = _host_page(share, row, is_unlocked(request, share),
                      tcfg.site_key if tcfg.enabled else "")
    return HTMLResponse(page, headers={"Cache-Control": "no-store",
                                       "Content-Security-Policy": _HOST_PAGE_CSP})


# Password attempts on one link run one at a time; the lock lives only
# while someone holds or waits for it.
_unlock_locks: dict[str, asyncio.Lock] = {}
_unlock_waiting: dict[str, int] = {}


@contextlib.asynccontextmanager
async def _one_unlock_at_a_time(share_id: str):
    lock = _unlock_locks.setdefault(share_id, asyncio.Lock())
    _unlock_waiting[share_id] = _unlock_waiting.get(share_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        left = _unlock_waiting[share_id] - 1
        if left:
            _unlock_waiting[share_id] = left
        else:
            _unlock_waiting.pop(share_id, None)
            _unlock_locks.pop(share_id, None)


class UnlockRequest(BaseModel):
    password: str = ""
    turnstile_token: str | None = None


@router.post("/s/{token}/unlock")
async def share_unlock(token: str, req: UnlockRequest, request: Request):
    """Password mode: a right password sets the link's own cookie. Wrong
    passwords count against the client IP and the link (ten in five
    minutes each); a right one clears the client's count. Attempts on one
    link run one at a time, so a burst cannot all pass the check before
    the first wrong one is counted."""
    _json_only(request)
    found = await asyncio.to_thread(load_live_link, token)
    if not found:
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    if not needs_password(share):
        return {"status": "ok", "release_sha": _release_sha(share, row)}
    ip = get_client_ip(request)
    async with _one_unlock_at_a_time(share["id"]):
        for bucket, key in (("share_unlock_ip", ip), ("share_unlock_share", share["id"])):
            allowed, retry_after = rate_limiter.check_rate_limit(bucket, key)
            if not allowed:
                raise HTTPException(status_code=429,
                                    detail=f"Too many attempts. Try again in {retry_after} seconds.",
                                    headers={"Retry-After": str(retry_after)})
        from services.infra import turnstile
        settings = await asyncio.to_thread(task_store.get_all_platform_settings)
        tcfg = turnstile.load_config(settings)
        if tcfg.enabled and not await turnstile.verify_token(tcfg, req.turnstile_token or "", ip):
            raise HTTPException(status_code=403, detail="Verification failed — reload and try again")
        try:
            ok = await verify_password_async(req.password or "", share.get("password_hash") or "")
        except HashBusy:
            raise _confirm.hash_busy()
        if not ok:
            rate_limiter.record_attempt("share_unlock_ip", ip)
            rate_limiter.record_attempt("share_unlock_share", share["id"])
            raise HTTPException(status_code=403, detail="Wrong password")
    rate_limiter.clear_rate_limit("share_unlock_ip", ip)
    response = JSONResponse({"status": "ok", "release_sha": _release_sha(share, row)})
    response.set_cookie(
        key=_cookie_name(share), value=_mint_unlock(share), httponly=True,
        secure=config.COOKIE_SECURE, samesite="lax", max_age=UNLOCK_TTL_S,
        path=f"/s/{token}",
    )
    return response


def _gate(request: Request, token: str) -> tuple[dict, dict] | None:
    """Sync: the live link, or None when it is gone; raises 401 when the
    password step is still due."""
    found = load_live_link(token)
    if not found:
        return None
    share, _row = found
    if not is_unlocked(request, share):
        raise HTTPException(status_code=401, detail="password required")
    return found


@router.get("/s/{token}/html")
async def share_html(token: str, request: Request):
    """The app itself, from its release copy, in the same sandbox the
    dashboard uses. Counted once per document fetch."""
    origin = request_origin(request)
    try:
        found = await asyncio.to_thread(_gate, request, token)
    except HTTPException:
        return _ui_response(_placeholder("Enter the link's password first."), origin, 401)
    if not found:
        return _ui_response(_placeholder("This link is not available."), origin, 404)
    share, row = found
    if share.get("target_kind") != "app":
        return _ui_response(_placeholder("This link is not available."), origin, 404)
    if db_apps.app_kind_of(row).serves_tree:
        await asyncio.to_thread(share_store.touch_access, share["id"])
        return await _folder_document_for_link(row, token, request)
    kind, content = await asyncio.to_thread(_app_document, row, None, False)
    if kind != "ok":
        return _ui_response(_placeholder("This link is not available."), origin, 404)
    await asyncio.to_thread(share_store.touch_access, share["id"])
    if is_full_document(content):
        return _ui_response(inject_runtime(content, runtime_extra=APP_RUNTIME), origin)
    return _ui_response(wrap_fragment(content, runtime_extra=APP_RUNTIME), origin)


# ── Folder apps on a link (APPS.md "External links") ───────────────────────
# The same routes the dashboard frame uses, under the link's own prefix: the
# document behind the unlock cookie, the assets on the release hash, the app
# API and socket behind an external viewer claim the page minted here. No
# platform methods, no push, no state writes, no catalog, no files.


async def _folder_document_for_link(row: dict, token: str, request: Request, sha: str = ""):
    from api.apps import app_proxy
    from services.apps import releases
    origin = request_origin(request)
    try:
        live = await asyncio.to_thread(releases.live_release_dir, row)
    except releases.ReleaseDamaged:
        live = None
    if live is None or (sha and sha != (row.get("release_sha256") or "")):
        return _ui_response(_placeholder("This link is not available."), origin, 404)
    return await asyncio.to_thread(app_proxy.document_response, row, live, origin, f"/s/{token}")


def _link_row(token: str) -> tuple[dict, dict] | None:
    found = load_live_link(token)
    if not found or found[0].get("target_kind") != "app" or not db_apps.app_kind_of(found[1]).serves_tree:
        return None
    return found


async def _link_caller(token: str, share: dict, row: dict, bearer: str):
    """The external claim this link's page minted: nothing else reaches the
    app through a link."""
    from api.apps import app_proxy
    caller = await app_proxy.caller_from_token(bearer, row)
    if caller.basis != "external" or caller.grant != share["id"]:
        raise HTTPException(status_code=401, detail="a link's viewer claim is required",
                            headers={"Access-Control-Allow-Origin": "*"})
    return caller


@router.post("/s/{token}/viewer-token")
async def share_viewer_token(token: str, request: Request):
    """The claim a folder app's page sends with its API calls from a link:
    an external viewer, the share recorded, ten minutes — and the app's own
    session token inside it when the link's cookie holds one. Paced per
    link and client IP (two seconds), not per link."""
    try:
        found = await asyncio.to_thread(_gate, request, token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="password required")
    if not found or found[0].get("target_kind") != "app":
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    _check_fire_rate(share["id"], "\x00viewer-token", get_client_ip(request), interval=2.0)
    return _external_claim(share, row, session_of(request, share))


class SessionRequest(BaseModel):
    token: str = ""


@router.post("/s/{token}/session")
async def share_session_set(token: str, req: SessionRequest, request: Request):
    """``otodock.session.set(token)`` from a link's page (APPS.md "External
    links"): the app's own session token goes into a cookie of the link's —
    HttpOnly, bound to the share, scoped to the link's path, alive for the
    manifest's ``session_days`` — and a fresh claim carrying it comes back.
    JSON and same-origin only, behind the unlock."""
    _json_only(request)
    try:
        found = await asyncio.to_thread(_gate, request, token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="password required")
    if not found or found[0].get("target_kind") != "app" or not db_apps.app_kind_of(found[1]).serves_tree:
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    if not _session_token_ok(req.token):
        raise HTTPException(status_code=400,
                            detail="a session token is printable text of at most 512 characters")
    _check_fire_rate(share["id"], "\x00session", get_client_ip(request), interval=1.0)
    days = _mf.parse_external(row)["session_days"]
    response = JSONResponse({"status": "ok", **_external_claim(share, row, req.token)})
    response.set_cookie(
        key=_session_cookie_name(share), value=_mint_session(share, req.token, days),
        httponly=True, secure=config.COOKIE_SECURE, samesite="lax", max_age=int(days) * 86400,
        path=f"/s/{token}",
    )
    return response


@router.delete("/s/{token}/session")
async def share_session_clear(token: str, request: Request):
    """``otodock.session.clear()``: the cookie goes, a fresh claim without
    a session comes back. Same-origin only."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="not available")
    try:
        found = await asyncio.to_thread(_gate, request, token)
    except HTTPException:
        raise HTTPException(status_code=401, detail="password required")
    if not found or found[0].get("target_kind") != "app" or not db_apps.app_kind_of(found[1]).serves_tree:
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    response = JSONResponse({"status": "ok", **_external_claim(share, row, "")})
    response.delete_cookie(key=_session_cookie_name(share), path=f"/s/{token}")
    return response


@router.options("/s/{token}/api/{path:path}")
async def share_api_preflight(token: str, path: str) -> Response:
    from api.apps import app_proxy
    return Response(status_code=204, headers=dict(app_proxy._CORS))


@router.api_route("/s/{token}/api/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def share_api(token: str, path: str, request: Request):
    from api.apps import app_proxy
    found = await asyncio.to_thread(_link_row, token)
    if not found:
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    caller = await _link_caller(token, share, row, app_proxy._bearer(request.headers))
    # The actor: the app session in the claim, else this client IP.
    caller.extra["ip"] = get_client_ip(request)
    rest = app_proxy._rest_path(request, row["id"], path, prefix=f"/s/{token}/api")
    app_proxy.check_app_path(path, rest)
    app_proxy.check_rate(row["id"], caller.actor)
    if request.method not in ("GET", "HEAD") and _mf.challenge_path(row, rest):
        await _require_challenge(request, row)
    return await app_proxy.proxy_request(row, caller, request, rest)


@router.websocket("/s/{token}/ws")
async def share_ws_root(websocket: WebSocket, token: str):
    await share_ws(websocket, token, "")


@router.websocket("/s/{token}/ws/{path:path}")
async def share_ws(websocket: WebSocket, token: str, path: str):
    from api.apps import app_proxy
    await websocket.accept()
    found = await asyncio.to_thread(_link_row, token)
    if not found:
        await app_proxy._close(websocket, 1008, "not available")
        return
    share, row = found
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=app_proxy.WS_AUTH_TIMEOUT_S)
        msg = json.loads(first)
        caller = await _link_caller(token, share, row, str((msg or {}).get("token") or ""))
        caller.extra["ip"] = get_client_ip(websocket)
        raw = websocket.scope.get("raw_path") or b""
        app_proxy.check_app_path(path, raw.decode("latin-1") if isinstance(raw, bytes) else str(raw))
    except HTTPException as e:
        await app_proxy._close(websocket, 4401 if e.status_code == 401 else 1008, str(e.detail))
        return
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await app_proxy._close(websocket, 4400, "auth frame expected")
        return
    await app_proxy.bridge_ws(websocket, row, path, caller)


@router.get("/s/{token}/client/{sha}/")
async def share_client_document(token: str, sha: str, request: Request):
    origin = request_origin(request)
    try:
        found = await asyncio.to_thread(_gate, request, token)
    except HTTPException:
        return _ui_response(_placeholder("Enter the link's password first."), origin, 401)
    if not found or found[0].get("target_kind") != "app" or not db_apps.app_kind_of(found[1]).serves_tree:
        return _ui_response(_placeholder("This link is not available."), origin, 404)
    share, row = found
    await asyncio.to_thread(share_store.touch_access, share["id"])
    return await _folder_document_for_link(row, token, request, sha)


@router.get("/s/{token}/client/{sha}/{path:path}")
async def share_client_asset(token: str, sha: str, path: str, request: Request):
    """A release file on the link token and the tree hash alone (the frame
    sends no cookie)."""
    from api.apps import app_proxy
    if path in ("", "index.html"):
        return await share_client_document(token, sha, request)
    found = await asyncio.to_thread(_link_row, token)
    if not found or sha != (found[1].get("release_sha256") or ""):
        # The live release only: a link never reaches a pending, preview or
        # older copy by its hash (the document route pins it the same way).
        raise HTTPException(status_code=404, detail="Not found")
    return await asyncio.to_thread(app_proxy.asset_response, found[1], sha, path)


@router.get("/s/{token}/snapshot")
async def share_snapshot(token: str, request: Request):
    """A chat link's snapshot (SHARING.md "Chat shares"): the copy made at
    share time. Counted once per fetch, like an app's document."""
    from services.sharing import chat_snapshot
    found = await asyncio.to_thread(_gate, request, token)
    if not found or found[0].get("target_kind") != "chat":
        raise HTTPException(status_code=404, detail="not available")
    share, _chat = found
    doc = await asyncio.to_thread(chat_snapshot.load, share)
    if not doc:
        raise HTTPException(status_code=404, detail="not available")
    await asyncio.to_thread(share_store.touch_access, share["id"])
    creator = await asyncio.to_thread(task_store.get_user, share.get("created_by") or "") or {}
    return {
        "title": doc.get("title") or "",
        "created_at": doc.get("created_at") or "",
        "shared_by_name": creator.get("display_name") or creator.get("name") or "",
        # A link viewer is anonymous: no author's internal id (an SSO
        # account's is its provider subject) leaves with the text.
        "messages": [{k: v for k, v in m.items() if k != "author_sub"}
                     for m in doc.get("messages") or []],
    }


@router.get("/s/{token}/ui/{media_token}")
async def share_snapshot_ui(token: str, media_token: str, request: Request):
    """An artifact page from a chat link's snapshot, in the artifact sandbox."""
    from services.sharing import chat_snapshot
    origin = request_origin(request)
    try:
        found = await asyncio.to_thread(_gate, request, token)
    except HTTPException:
        return _ui_response(_placeholder("Enter the link's password first."), origin, 401)
    if not found or found[0].get("target_kind") != "chat":
        return _ui_response(_placeholder("This link is not available."), origin, 404)
    path = await asyncio.to_thread(chat_snapshot.file_path, found[0], media_token)
    if path is None:
        return _ui_response(_placeholder("This artifact no longer exists."), origin, 404)
    content = await asyncio.to_thread(path.read_text, "utf-8", "replace")
    if is_full_document(content):
        return _ui_response(inject_runtime(content), origin)
    return _ui_response(wrap_fragment(content), origin)


@router.get("/s/{token}/media/{media_token}")
async def share_snapshot_media(token: str, media_token: str, request: Request):
    """An image, clip or file from a chat link's snapshot; inert types
    inline, the rest as a download."""
    import mimetypes
    from fastapi.responses import FileResponse
    from services.sharing import chat_snapshot
    found = await asyncio.to_thread(_gate, request, token)
    if not found or found[0].get("target_kind") != "chat":
        raise HTTPException(status_code=404, detail="not available")
    path = await asyncio.to_thread(chat_snapshot.file_path, found[0], media_token)
    if path is None:
        raise HTTPException(status_code=404, detail="not available")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    if chat_snapshot.inline_ok(mime):
        return FileResponse(path, media_type=mime, headers=headers)
    return FileResponse(path, media_type=mime, filename=path.name, headers=headers)


@router.get("/s/{token}/state")
async def share_state(token: str, request: Request):
    """The state document as it stands when the page loads; a link gets no
    live delivery (APPS.md "Live apps")."""
    found = await asyncio.to_thread(_gate, request, token)
    if not found or found[0].get("target_kind") != "app":
        raise HTTPException(status_code=404, detail="not available")
    _share, row = found
    doc, rev = await asyncio.to_thread(task_store.get_app_state, row["id"])
    return {"doc": doc, "rev": rev}


class LinkBatchCall(BaseModel):
    call_id: str
    action_id: str
    args: Any = None


class LinkBatchRequest(BaseModel):
    calls: list[LinkBatchCall]


async def _run_link_action(share: dict, row: dict, action_id: str, args, ip: str) -> dict:
    """One action from a link: the Buttons switch, the kind, the daily cap,
    then the executor with the share as the actor. Every refusal is an
    HTTPException."""
    if not share.get("allow_actions"):
        raise HTTPException(status_code=403, detail="not available on shared links")
    action = _mf.find_action(row, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Unknown action")
    if action.get("type") not in ("mcp_tool", "fire_task"):
        raise HTTPException(status_code=403, detail="not available on shared links")
    count = await asyncio.to_thread(share_store.bump_actions_today, share["id"], _today(),
                                    ACTIONS_PER_DAY)
    if count is None:
        raise HTTPException(status_code=429, detail="this link reached its daily limit")
    args_hash = hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    out = await _run_action(row, action_id, args, None, actor=f"share:{share['id']}")
    logger.info(f"Share action: share={share['id']}, app={row.get('slug')}, action={action_id}, "
                f"args={args_hash}, ip={ip}, count={count}")
    return out


@router.post("/s/{token}/actions/batch")
async def share_actions_batch(token: str, req: LinkBatchRequest, request: Request):
    """The host page's batch: one line per call, a refusal is that entry's
    ``denied`` line, never an HTTP error for the batch."""
    _json_only(request)
    found = await asyncio.to_thread(_gate, request, token)
    if not found or found[0].get("target_kind") != "app":
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    calls = req.calls
    if not 1 <= len(calls) <= 16:
        raise HTTPException(status_code=400, detail="a batch carries 1 to 16 calls")
    _check_fire_rate(row["id"], "\x00batch", f"share:{share['id']}", interval=1.0)
    ip = get_client_ip(request)
    fire_seen = False
    entries: list[tuple[LinkBatchCall, bool]] = []
    for call in calls:
        action = _mf.find_action(row, call.action_id) or {}
        fires = action.get("type") == "fire_task"
        entries.append((call, fires and fire_seen))
        fire_seen = fire_seen or fires

    async def _one(call: LinkBatchCall, extra_fire: bool) -> dict:
        out: dict = {"call_id": call.call_id, "action_id": call.action_id}
        try:
            if extra_fire:
                raise HTTPException(status_code=429, detail="One task run per batch")
            out.update(await _run_link_action(share, row, call.action_id, call.args, ip))
        except HTTPException as e:
            out.update({"status": "denied", "reason": e.detail, "code": e.status_code})
        return out

    tasks = [asyncio.ensure_future(_one(c, extra)) for c, extra in entries]

    async def _lines():
        for fut in asyncio.as_completed(tasks):
            yield json.dumps(await fut, separators=(",", ":"), default=str) + "\n"

    return StreamingResponse(
        _lines(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class LinkActionRequest(BaseModel):
    args: Any = None


@router.post("/s/{token}/actions/{action_id}")
async def share_action(token: str, action_id: str, request: Request,
                       req: LinkActionRequest | None = None):
    _json_only(request)
    found = await asyncio.to_thread(_gate, request, token)
    if not found or found[0].get("target_kind") != "app":
        raise HTTPException(status_code=404, detail="not available")
    share, row = found
    return await _run_link_action(share, row, action_id, req.args if req else None,
                                  get_client_ip(request))


@router.api_route("/s/{token}/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def share_unknown(token: str, rest: str) -> Response:
    """Anything else under a link is the same 404 page (never the dashboard
    shell, never a hint about the token)."""
    return _not_available_page()
