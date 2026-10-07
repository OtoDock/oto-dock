"""Push notification sender.

Handles Web Push (VAPID) and FCM (Firebase) push delivery.
Web Push uses pywebpush; FCM uses google-auth + httpx.
Both are optional — if libraries/config are missing, calls are no-ops.
"""

import asyncio
import time
import functools
import json
import logging
import ssl
from urllib.parse import urlsplit

import config
from services.infra.outbound_url import validate_outbound_url
from storage.automation import notification_store

logger = logging.getLogger("claude-proxy.push")


def _endpoint_is_public(endpoint: str) -> bool:
    """SSRF guard for a user-supplied Web Push endpoint: ``https`` and EVERY
    address the host resolves to publicly routable
    (``services.infra.outbound_url``), so a member can't point the proxy at
    ``169.254.169.254`` / loopback / an internal host via a notification
    they trigger themselves. A backslash or a userinfo part is refused
    first: ``requests`` reads the host of such a URL differently from the
    validator's ``urlsplit`` (``https://10.0.0.5\\@example.com/`` connects to
    10.0.0.5), and no push service issues one."""
    if "\\" in endpoint or "@" in urlsplit(endpoint).netloc:
        return False
    return validate_outbound_url(endpoint, require_https=True) is None


def _no_redirect_session():
    """The session a Web Push POST goes through: a redirect raises instead
    of being followed, so a validated public endpoint cannot bounce the
    request to an internal address."""
    import requests
    session = requests.Session()
    session.max_redirects = 0
    return session


# --- Web Push (VAPID) ---

_webpush_available = False
try:
    from pywebpush import webpush
    _webpush_available = True
except ImportError:
    logger.info("pywebpush not installed — Web Push disabled")


async def send_web_push(subscription_data: str, payload: dict) -> bool:
    """Send a Web Push notification via VAPID.

    subscription_data is a JSON string containing {endpoint, keys: {p256dh, auth}}.
    Returns True if sent successfully.
    """
    if not _webpush_available:
        return False
    if not config.VAPID_PRIVATE_KEY or not config.VAPID_PUBLIC_KEY:
        return False

    try:
        sub_info = json.loads(subscription_data)
        endpoint = sub_info.get("endpoint", "") if isinstance(sub_info, dict) else ""
        # SSRF guard: only deliver to a public https endpoint (DNS resolved +
        # checked off-thread so we never POST to an internal address).
        if not await asyncio.to_thread(_endpoint_is_public, endpoint):
            logger.warning("Refusing Web Push to non-public endpoint: %s", endpoint[:80])
            return False
        with _no_redirect_session() as session:
            await asyncio.to_thread(
                webpush,
                subscription_info=sub_info,
                data=json.dumps(payload),
                vapid_private_key=config.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{config.VAPID_EMAIL}"},
                timeout=10,
                requests_session=session,
            )
        return True
    except Exception as e:
        error_str = str(e)
        # 410 Gone or 404 = subscription expired, clean up
        if "410" in error_str or "404" in error_str:
            logger.info(f"Push subscription expired, removing: {error_str[:100]}")
            await asyncio.to_thread(
                notification_store.delete_push_subscription_by_data, subscription_data
            )
        else:
            logger.warning(f"Web Push failed: {error_str[:200]}")
        return False


# --- FCM (Firebase Cloud Messaging) ---

_fcm_available = False
_fcm_project_id = ""

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account

    fcm_path = getattr(config, "FCM_SERVICE_ACCOUNT_PATH", "")
    if fcm_path:
        _fcm_credentials = service_account.Credentials.from_service_account_file(
            fcm_path,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"],
        )
        # Extract project ID from service account
        with open(fcm_path) as f:
            sa_data = json.load(f)
            _fcm_project_id = sa_data.get("project_id", "")
        _fcm_available = True
        logger.info(f"FCM enabled for project: {_fcm_project_id}")
except Exception as e:
    logger.info(f"FCM not configured: {e}")

# google-auth's transport waits 120 s per attempt by default; the refresh
# runs in a worker thread under the lock below, so its wait is bounded.
_FCM_TOKEN_TIMEOUT_S = 15


class _FcmLoopState:
    """The sender's per-loop state: one refresh in flight, one HTTP client.
    Rebuilt when the running loop changes (the test suite runs several)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.token_lock: asyncio.Lock | None = None
        self.client_lock: asyncio.Lock | None = None
        self.client = None

    def current(self) -> "_FcmLoopState":
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.token_lock = asyncio.Lock()
            self.client_lock = asyncio.Lock()
            self.client = None
        return self


_fcm_state = _FcmLoopState()
_ssl_context: ssl.SSLContext | None = None


def reset_fcm_state() -> None:
    """Drop the per-loop lock and client (tests)."""
    global _fcm_state
    _fcm_state = _FcmLoopState()


def _refresh_fcm_token_blocking() -> None:
    request = functools.partial(GoogleAuthRequest(), timeout=_FCM_TOKEN_TIMEOUT_S)
    _fcm_credentials.refresh(request)


async def _fcm_access_token() -> str:
    """The service account's access token: cached while google-auth reports
    it valid (about an hour), refreshed in a worker thread with one refresh
    in flight, the other pushes waiting for its result."""
    if _fcm_credentials.valid:
        return _fcm_credentials.token
    async with _fcm_state.current().token_lock:
        if not _fcm_credentials.valid:
            await asyncio.to_thread(_refresh_fcm_token_blocking)
    return _fcm_credentials.token


def _build_ssl_context() -> ssl.SSLContext:
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


async def _fcm_http_client():
    """One ``httpx.AsyncClient`` per loop. The SSL context is what makes a
    client construction cost several milliseconds on the loop: built once,
    in a thread."""
    import httpx

    global _ssl_context
    state = _fcm_state.current()
    if state.client is None:
        async with state.client_lock:
            if state.client is None:
                if _ssl_context is None:
                    _ssl_context = await asyncio.to_thread(_build_ssl_context)
                state.client = httpx.AsyncClient(verify=_ssl_context, timeout=10)
    return state.client


async def _send_fcm_direct(token: str, payload: dict) -> bool:
    """BYO-Firebase **direct** FCM send — the escape hatch.

    Used only when a self-hoster supplied their own ``FCM_SERVICE_ACCOUNT_PATH``
    (and rebuilt the app against their own Firebase project so device tokens live
    in it). The **default** path is the relay (:func:`_send_fcm_relay`), which
    holds OtoDock's service account for the project the shipped app registers to.

    token is the FCM registration token from the device.
    Returns True if sent successfully.
    """
    if not _fcm_available:
        return False

    try:
        access_token = await _fcm_access_token()

        # Use data-only message (no "notification" key) so our custom
        # FirebaseMessagingService always handles it — even in background.
        # This lets us control TTS, alarm loops, and custom notification display.
        message = {
            "message": {
                "token": token,
                "data": {
                    "title": payload.get("title", ""),
                    "body": payload.get("body", ""),
                    "delivery_id": payload.get("delivery_id", ""),
                    "severity": payload.get("severity", "info"),
                    "ephemeral": str(payload.get("ephemeral", False)).lower(),
                    "click_url": payload.get("click_url", "/"),
                    "install_id": payload.get("install_id", ""),
                },
                "android": {
                    "priority": "high",
                },
            }
        }

        client = await _fcm_http_client()
        resp = await client.post(
            f"https://fcm.googleapis.com/v1/projects/{_fcm_project_id}/messages:send",
            json=message,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        elif resp.status_code == 404:
            # Token invalid, clean up
            logger.info("FCM token invalid, removing")
            await asyncio.to_thread(
                notification_store.delete_push_subscription_by_data, token
            )
        elif resp.status_code == 401:
            # A rotated or revoked key: the cached token must not keep
            # failing until it expires.
            _fcm_credentials.token = None
            logger.warning("FCM refused the access token; the next push refreshes it")
        else:
            logger.warning(f"FCM send failed: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"FCM error: {e}")
        return False


async def _send_fcm_relay(platform: str, token: str, payload: dict) -> bool:
    """Default native-push path: ask the OtoDock relay to send via FCM.

    The relay holds OtoDock's FCM service account (never shipped in any install).
    Covers Android and iOS (both register FCM tokens via the Firebase SDK). A
    ``token_invalid`` rejection means the device token is stale → drop the
    subscription, mirroring the direct path's 404 cleanup. Any other rejection /
    outage is non-fatal (push is best-effort)."""
    from services.billing import relay_client

    try:
        await relay_client.push_send(
            platform=platform, device_token=token, payload=payload,
        )
        return True
    except relay_client.RelayError as e:
        if e.code == "token_invalid":
            logger.info("Relay reports FCM token invalid, removing")
            await asyncio.to_thread(
                notification_store.delete_push_subscription_by_data, token
            )
        else:
            logger.warning(f"Relay push rejected: {e.code}")
        return False
    except relay_client.RelayNotConfigured:
        return False
    except Exception as e:
        logger.warning(f"Relay push failed: {e}")
        return False


_RELAY_VERDICT_TTL_S = 60.0
_relay_verdict: tuple[bool, float] | None = None


async def _relay_available() -> bool:
    """``relay_client.is_available()`` (a licence read and check) served
    from a 60 s cache, refreshed on ``run_db``: a fan-out of N pushes reads
    it once, not per push."""
    global _relay_verdict
    now = time.monotonic()
    if _relay_verdict is not None and now - _relay_verdict[1] < _RELAY_VERDICT_TTL_S:
        return _relay_verdict[0]
    from services.billing import relay_client
    from storage.pg import run_db

    verdict = bool(await run_db(relay_client.is_available))
    _relay_verdict = (verdict, now)
    return verdict


def reset_relay_verdict() -> None:
    global _relay_verdict
    _relay_verdict = None


async def send_fcm(token: str, payload: dict, platform: str = "android") -> bool:
    """Send a native (Android/iOS) push. **BYO direct → relay → no-op.**

    If the self-hoster configured their own ``FCM_SERVICE_ACCOUNT_PATH`` we send
    direct (the escape hatch); otherwise we route through the OtoDock relay (the
    default for the shipped app); if neither is available it's a no-op. Web Push
    is handled separately (:func:`send_web_push`) and is always local."""
    if _fcm_available:
        return await _send_fcm_direct(token, payload)
    if await _relay_available():
        return await _send_fcm_relay(platform, token, payload)
    return False


# --- Unified sender ---

# The sends' concurrency, per loop: a semaphore of its own, never the
# notification fan-out's recipient slot (``fire_notification`` holds one
# across ``send_to_user``; the same semaphore inside would deadlock once
# every recipient slot waits on a send slot).
_PUSH_CONCURRENCY = 8
_push_slots: dict[int, asyncio.Semaphore] = {}


def _push_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _push_slots.get(id(loop))
    if slot is None:
        _push_slots.clear()
        slot = _push_slots[id(loop)] = asyncio.Semaphore(_PUSH_CONCURRENCY)
    return slot


async def send_to_user(user_sub: str, payload: dict) -> None:
    """Send push notification to all of a user's registered subscriptions,
    at once (a Web Push send holds a thread for up to 10 s; the sends'
    bound keeps that in check)."""
    from storage.pg import run_db

    subscriptions = await run_db(notification_store.get_push_subscriptions, user_sub)
    if not subscriptions:
        return
    slot = _push_slot()

    async def _one(sub: dict) -> None:
        platform = sub["platform"]
        data = sub["subscription_data"]
        async with slot:
            if platform == "web":
                await send_web_push(data, payload)
            elif platform in ("android", "ios"):
                await send_fcm(data, payload, platform)

    await asyncio.gather(*(_one(s) for s in subscriptions), return_exceptions=True)
