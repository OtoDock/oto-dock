"""Push notification sender.

Handles Web Push (VAPID) and FCM (Firebase) push delivery.
Web Push uses pywebpush; FCM uses google-auth + httpx.
Both are optional — if libraries/config are missing, calls are no-ops.
"""

import asyncio
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import config
from storage.automation import notification_store

logger = logging.getLogger("claude-proxy.push")


def _ip_blocked(ip_str: str) -> bool:
    """True if an IP is non-public (loopback / RFC1918 / link-local / reserved /
    unspecified / multicast), incl. IPv4-mapped IPv6 and the cloud-metadata
    address (169.254.169.254 → link-local). Unparseable → blocked."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_unspecified or ip.is_multicast
    )


def _endpoint_is_public(endpoint: str) -> bool:
    """SSRF guard for a user-supplied Web Push endpoint: require ``https`` and
    confirm EVERY address the host resolves to is publicly routable, so a member
    can't point the proxy at ``169.254.169.254`` / loopback / an internal host
    via a notification they trigger themselves."""
    if not endpoint:
        return False
    try:
        u = urlparse(endpoint)
    except Exception:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    if not infos:
        return False
    return not any(_ip_blocked(info[4][0]) for info in infos)

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
        await asyncio.to_thread(
            webpush,
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=config.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{config.VAPID_EMAIL}"},
            timeout=10,
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

    import httpx

    try:
        # Refresh credentials
        _fcm_credentials.refresh(GoogleAuthRequest())
        access_token = _fcm_credentials.token

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

        async with httpx.AsyncClient() as client:
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
                logger.info(f"FCM token invalid, removing")
                await asyncio.to_thread(
                    notification_store.delete_push_subscription_by_data, token
                )
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


async def send_fcm(token: str, payload: dict, platform: str = "android") -> bool:
    """Send a native (Android/iOS) push. **BYO direct → relay → no-op.**

    If the self-hoster configured their own ``FCM_SERVICE_ACCOUNT_PATH`` we send
    direct (the escape hatch); otherwise we route through the OtoDock relay (the
    default for the shipped app); if neither is available it's a no-op. Web Push
    is handled separately (:func:`send_web_push`) and is always local."""
    if _fcm_available:
        return await _send_fcm_direct(token, payload)
    from services.billing import relay_client

    if relay_client.is_available():
        return await _send_fcm_relay(platform, token, payload)
    return False


# --- Unified sender ---

async def send_to_user(user_sub: str, payload: dict) -> None:
    """Send push notification to all of a user's registered subscriptions."""
    subscriptions = await asyncio.to_thread(
        notification_store.get_push_subscriptions, user_sub
    )
    if not subscriptions:
        return

    for sub in subscriptions:
        platform = sub["platform"]
        data = sub["subscription_data"]

        if platform == "web":
            await send_web_push(data, payload)
        elif platform in ("android", "ios"):
            await send_fcm(data, payload, platform)
