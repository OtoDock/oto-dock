"""A person's ``otok_`` key as the principal of the public routes.

The middleware calls ``gate`` for every HTTP request (``middleware.py``),
after its synchronous judges and before any body is read:

- a key bearer on a route ``auth/api_key_endpoints.py`` does not list: 403;
- on a listed route: the token is looked up in the verified cache first
  (an HMAC of it under a per-process secret, read on the loop). A key
  found there is charged to its own call bucket, by key id, and skips the
  address's failure bucket; any other token meets that bucket before
  anything is read. Then the key, off the event loop (the cached entry
  re-checked against its row; a miss is one bcrypt in a worker thread
  behind a small semaphore with a bounded queue), its standing
  (``api_key_manager.key_row_refusal``: revoked, switched off, expired, the
  person's sign-in), the person's gate and the route's scope. Only a token
  that matches no key counts against its address: a matched key that is
  refused was no guess. A key checked for the first time is charged to its
  call bucket after the check, so no caller charges a key it does not
  hold. The principal is left where ``get_current_user`` memoizes it, so
  the route reads it as any other;
- a ``/v1/api/`` route without a key: a cookie reaches only the description
  and the command, nothing else answers but 401.

One log line per call names the key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import OrderedDict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import config
from auth import api_key_endpoints as endpoints, rate_limiter
from auth.providers import UserContext, auth_gate

logger = logging.getLogger("otodock.api-keys")

_TTL_S = 300.0
_CACHE_MAX = 4096
_HMAC_KEY = secrets.token_bytes(32)
# HMAC of the token → (key id, stored hash, until). Read and written on
# worker threads and dropped from the loop: every touch holds ``_lock``.
_verified: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
_lock = threading.Lock()
# The last ``last_used_at`` write per key (one a minute at most).
_touched: dict[str, float] = {}
_TOUCH_EVERY_S = 60.0

NO_KEY = "This route takes a personal API key."
NOT_LISTED = "An API key cannot use this route."
TRIGGERS_ONLY = "This key fires triggers only. Make a new key to use the API."


class KeyRefused(Exception):
    def __init__(self, status: int, detail: str, *, failed: bool = True,
                 headers: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.failed = failed
        self.headers = headers or {}


class _Gate:
    """The verification slots, bound to the running loop (the test suite
    runs several loops)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sem: asyncio.Semaphore | None = None
        self.waiting = 0

    def current(self) -> _Gate:
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.sem = asyncio.Semaphore(config.WEBHOOK_KEY_VERIFY_CONCURRENCY)
            self.waiting = 0
        return self


_slots = _Gate()


def _digest(token: str) -> str:
    return hmac.new(_HMAC_KEY, token.encode("utf-8"), hashlib.sha256).hexdigest()


def forget(key_id: str = "") -> None:
    """Drop the cached verdicts (one key's, or all): a revocation needs no
    call here (every hit re-reads the row), a test does."""
    with _lock:
        if not key_id:
            _verified.clear()
            return
        for digest in [d for d, v in _verified.items() if v[0] == key_id]:
            _verified.pop(digest, None)


def _cached(digest: str) -> tuple[str, str, float] | None:
    """The live entry for this digest (an old one is dropped)."""
    with _lock:
        hit = _verified.get(digest)
        if hit is not None and hit[2] <= time.monotonic():
            _verified.pop(digest, None)
            return None
        return hit


def cached_key_id(token: str) -> str:
    """The id of the key a live verdict holds for this token, "" for none.
    No read past the cache: for the gate, on the loop."""
    hit = _cached(_digest(token))
    return hit[0] if hit else ""


def _cached_row(digest: str) -> dict | None:
    from storage.identity import api_key_store
    hit = _cached(digest)
    if hit is None:
        return None
    row = api_key_store.get_user_api_key(hit[0])
    if not row or row.get("key_hash") != hit[1]:
        with _lock:
            _verified.pop(digest, None)
        return None
    # A key in use stays known: its five minutes start again.
    _remember(digest, row)
    return row


def _remember(digest: str, row: dict) -> None:
    with _lock:
        _verified[digest] = (row["id"], row["key_hash"], time.monotonic() + _TTL_S)
        _verified.move_to_end(digest)
        while len(_verified) > _CACHE_MAX:
            _verified.popitem(last=False)


def _bcrypt_row(token: str) -> dict | None:
    from services.infra import api_key_manager
    from storage.identity import api_key_store
    body = token[len(api_key_manager.KEY_PUBLIC_PREFIX):]
    for row in api_key_store.get_user_keys_by_prefix(body[:api_key_manager.KEY_INDEX_PREFIX_LEN]):
        if api_key_manager._check_key(token, row["key_hash"]):
            return row
    return None


def _principal(row: dict, scope_needed: str) -> UserContext:
    """The person behind a found key row, or the refusal. Synchronous: on
    a worker thread (it reads the person's rows)."""
    from auth.providers import _load_user
    from services.infra import api_key_manager
    from storage.identity import api_key_store
    user, agent_roles, _ = _load_user(row["user_sub"], with_default=False)
    refusal = api_key_manager.key_row_refusal(row, api_key_manager.KEY_KIND_USER, user)
    if refusal or not user:
        # A key that matched and has ended is no failed guess.
        raise KeyRefused(401, refusal or "This key's account is gone.", failed=False)
    if api_key_manager.is_pre_2_0(row):
        # No end date, and no sign-in ends it: it keeps what it had, the
        # person's webhook fires, and reaches no public route.
        raise KeyRefused(403, TRIGGERS_ONLY, failed=False)
    if auth_gate(user):
        raise KeyRefused(403, "This key's account must change its password or set up "
                              "two-step sign-in first.", failed=False)
    scopes = tuple(api_key_store.parse_permissions(row.get("permissions")))
    if scope_needed and scope_needed not in scopes:
        raise KeyRefused(403, f"This key does not have the {scope_needed} permission.",
                         failed=False)
    return UserContext(
        sub=user["sub"], email=user["email"], name=user["name"], role=user["role"],
        agents=list(agent_roles.keys()), display_name=user.get("display_name", ""),
        agent_roles=agent_roles, is_owner=bool(user.get("is_owner")), is_api_key=True,
        api_key_id=row["id"], api_key_name=row.get("name") or "",
        api_key_scopes=scopes,
        api_key_agents=tuple(api_key_store.parse_agents(row.get("agents"))),
        api_key_runs_unasked=bool(row.get("run_without_asking")),
    )


def _verify_cached(token: str, scope_needed: str) -> UserContext | None:
    row = _cached_row(_digest(token))
    return None if row is None else _principal(row, scope_needed)


def _verify_slow(token: str, scope_needed: str) -> UserContext:
    row = _bcrypt_row(token)
    if row is None:
        raise KeyRefused(401, "This key is not valid.")
    # Known before it is judged: a matched key that is refused costs no
    # bcrypt at its next call, and that call is charged to the key.
    _remember(_digest(token), row)
    return _principal(row, scope_needed)


async def verify(token: str, scope_needed: str) -> UserContext:
    """The key's principal for a route needing ``scope_needed``, or
    ``KeyRefused``. Every read and the bcrypt run off the loop."""
    from services.infra import api_key_manager
    if config.is_master_key(token):
        raise KeyRefused(401, "This key is not valid.")
    body = token[len(api_key_manager.KEY_PUBLIC_PREFIX):]
    if len(body) < api_key_manager.KEY_INDEX_PREFIX_LEN:
        raise KeyRefused(401, "This key is not valid.")
    principal = await asyncio.to_thread(_verify_cached, token, scope_needed)
    if principal is not None:
        return principal
    gate = _slots.current()
    sem = gate.sem
    assert sem is not None  # current() bound it
    if sem.locked() and gate.waiting >= config.WEBHOOK_KEY_VERIFY_MAX_WAITERS:
        raise KeyRefused(503, "The server is busy. Try again in a few seconds.", failed=False,
                         headers={"Retry-After": "5"})
    gate.waiting += 1
    try:
        await sem.acquire()
    finally:
        gate.waiting -= 1
    try:
        return await asyncio.to_thread(_verify_slow, token, scope_needed)
    finally:
        sem.release()


def _touch(key_id: str) -> None:
    now = time.monotonic()
    if now - _touched.get(key_id, 0.0) < _TOUCH_EVERY_S:
        return
    if len(_touched) > _CACHE_MAX:
        _touched.clear()
    _touched[key_id] = now
    from services.infra import api_key_manager
    api_key_manager._schedule_last_used_update("user", key_id)


def _refusal(status: int, detail: str, headers: dict | None = None) -> Response:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def _charge(key_id: str) -> Response | None:
    """One call against the key's own bucket: the 429 past its cap."""
    ok, retry = rate_limiter.hit("api_key_call", key_id)
    if ok:
        return None
    return _refusal(429, "This key sent too many requests. Try again later.",
                    {"Retry-After": str(retry)})


async def gate(request: Request) -> Response | None:
    """The middleware's judgement of a key bearer and of the public routes
    (module docstring). None lets the request go on."""
    from middleware import routed_path
    authorization = request.headers.get("authorization", "")
    path = routed_path(request)
    method = request.scope.get("method", "")
    if not endpoints.is_key_bearer(authorization):
        if not path.startswith(endpoints.PUBLIC_PREFIX):
            return None
        if path in endpoints.COOKIE_PATHS and method == "GET" and request.cookies.get("session"):
            return None
        return _refusal(401, NO_KEY)
    route = endpoints.key_route(method, path)
    if route is None:
        logger.info("API key refused off its list: %s %s", method, path)
        return _refusal(403, NOT_LISTED)
    if not route.verified:
        return None
    from auth.lan_check import auth_bucket_key
    address = auth_bucket_key(request)
    token = authorization[7:].strip()
    known = cached_key_id(token)
    if known:
        refusal = _charge(known)
    else:
        ok, retry = rate_limiter.check_rate_limit("api_key_fail", address)
        refusal = None if ok else _refusal(
            429, "Too many failed keys from this address. Try again later.",
            {"Retry-After": str(retry)})
    if refusal is not None:
        return refusal
    try:
        principal = await verify(token, route.scope)
    except KeyRefused as e:
        if e.failed:
            rate_limiter.record_attempt("api_key_fail", address)
        logger.info("API key refused (%s): %s %s", e.detail, method, path)
        return _refusal(e.status, e.detail, e.headers)
    if not known:
        refusal = _charge(principal.api_key_id)
        if refusal is not None:
            return refusal
    request.scope.setdefault("state", {})["otodock_principal"] = principal
    request.scope["otodock.api_key"] = principal.api_key_id
    logger.info("API key %s (%s): %s %s", principal.api_key_id[:8], principal.sub[:8],
                method, path)
    _touch(principal.api_key_id)
    return None

