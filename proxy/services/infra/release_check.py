"""The daily release check: admins are told when a newer OtoDock release exists
(features/BACKUP-RESTORE.md "Admins are told a release exists").

Once a day the worker asks ``OTODOCK_RELEASE_CHECK_URL`` (GitHub's
``releases/latest`` answer by default) and compares the tag with the running
version under the one version rule (``storage.schema.parse_version``). A newer
release is stored for the admin banner and the OtoDock tab, and announced to
admins once per version through a notification. Off under
``OTODOCK_AIR_GAPPED``, on the cloud and when ``OTODOCK_RELEASE_CHECK`` is
false, judged at every call, so a switch flipped after a first check hides
the stored facts at once.

The fetch goes through ``pinned_http.checked_client``: the default URL dials
public addresses only; an operator-set URL is an admin-configured endpoint
and may be private (a mirror, a stand-in). A failure (a rate-limited 403, a
network error, a malformed body) is recorded and the hourly tick retries; the
last facts stay. Facts fetched from another URL than the configured one are
discarded, so a changed URL leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

import config
from services.infra import pinned_http
from storage.pg import get_conn, run_db
from storage.schema import parse_version

logger = logging.getLogger("otodock.release-check")

FIRST_TICK_S = 90
TICK_S = 3600
CHECK_EVERY = timedelta(hours=24)
COOLDOWN_S = 60
BODY_CAP = 256 * 1024
_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(-rc\d+)?$")
#: The notification source of the "a release exists" notice.
SOURCE = "release"
#: The public upgrading guide: the notification and the admin banner point at it.
UPGRADE_GUIDE_URL = "https://docs.otodock.io/administration/upgrading"

# platform_settings keys
LAST_AT = "release_check_last_at"
URL_KEY = "release_check_url"
LATEST = "release_latest_version"
LATEST_URL = "release_latest_url"
PUBLISHED = "release_latest_published_at"
ERROR = "release_check_error"
NOTIFIED = "release_notified_version"
_KEYS = (LAST_AT, URL_KEY, LATEST, LATEST_URL, PUBLISHED, ERROR, NOTIFIED)

_worker_task: asyncio.Task | None = None
_lock = asyncio.Lock()
_last_run_monotonic: float | None = None


class ReleaseCheckError(Exception):
    """The answer could not be used; the text is what the admin sees."""


def disabled_reason() -> str:
    """Why the check is off, or '' when it runs."""
    if config.OTODOCK_CLOUD:
        return "the operator upgrades the OtoDock cloud"
    if config.OTODOCK_AIR_GAPPED:
        return "OTODOCK_AIR_GAPPED is set"
    if not config.RELEASE_CHECK_ENABLED:
        return "OTODOCK_RELEASE_CHECK is off"
    if parse_version(config.PINNED_OTODOCK_VERSION) is None:
        return f"the running version {config.PINNED_OTODOCK_VERSION!r} is not a release number"
    return ""


def enabled() -> bool:
    return disabled_reason() == ""


def _shown(url: str) -> str:
    """The URL as an error names it: a mirror's user:password never shown; a
    URL that does not parse (a bad port, an unclosed bracket) is shown as its
    scheme and host, or as it was written when even those fail."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return url.split("@")[-1] if "@" in url else url
    try:
        port = parts.port
    except ValueError:
        return f"{parts.scheme}://{host}"
    if port:
        host = f"{host}:{port}"
    return parts._replace(netloc=host).geturl()


def _parsed(url: str) -> None:
    """``ReleaseCheckError`` unless the URL has a host and a port in range:
    httpx raises ``InvalidURL`` (not an ``HTTPError``) on one, and ``urlsplit``
    a bare ``ValueError``, so the check would crash instead of recording the
    error. The scheme is the client's to judge (``UnsupportedProtocol``)."""
    try:
        parts = urlsplit(url)
        if not parts.hostname or parts.port == -1:
            raise ValueError("no host")
    except ValueError as e:
        raise ReleaseCheckError(f"the URL {_shown(url)!r} is not valid") from e


def _policy(url: str) -> pinned_http.Policy:
    """Public addresses only for the default URL; an operator-set URL is an
    admin-configured endpoint, as OTODOCK_RELAY_BASE is, and may be private."""
    if url == config.RELEASE_CHECK_DEFAULT_URL:
        return pinned_http.public_only
    return lambda address: False


async def fetch_latest(url: str) -> dict:
    """``{version, url, published_at}`` from a ``releases/latest`` answer, or
    ``ReleaseCheckError``."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"OtoDock/{config.PINNED_OTODOCK_VERSION or 'dev'}",
    }
    _parsed(url)
    try:
        async with pinned_http.checked_client(
                url, _policy(url), timeout=10.0, follow_redirects=False, headers=headers) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ReleaseCheckError(f"HTTP {resp.status_code} from {_shown(url)}")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > BODY_CAP:
                        raise ReleaseCheckError("the answer is larger than 256 KB")
    except ReleaseCheckError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as e:
        raise ReleaseCheckError(f"{type(e).__name__}: {str(e).replace(url, _shown(url))}") from e
    try:
        data = json.loads(body)
    except ValueError as e:
        raise ReleaseCheckError("the answer is not JSON") from e
    if not isinstance(data, dict):
        raise ReleaseCheckError("the answer is not a release object")
    tag = str(data.get("tag_name") or "").strip()
    if not _TAG_RE.match(tag) or parse_version(tag) is None:
        raise ReleaseCheckError(f"tag_name {tag!r} is not a release number")
    html_url = str(data.get("html_url") or "").strip()
    if urlsplit(html_url).scheme != "https" or not urlsplit(html_url).netloc:
        raise ReleaseCheckError("html_url is not an https address")
    return {
        "version": tag.lstrip("v"),
        "url": html_url,
        "published_at": str(data.get("published_at") or ""),
    }


# --- the stored state (sync, run through run_db) ----------------------------

def _state() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM platform_settings WHERE key = ANY(%s)", (list(_KEYS),)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def _store(values: dict[str, str]) -> None:
    with get_conn() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO platform_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, value))
        conn.commit()


def _facts(state: dict[str, str]) -> dict[str, str]:
    """The latest-release facts, empty when they came from another URL."""
    if state.get(URL_KEY, "") != config.RELEASE_CHECK_URL:
        return {}
    return {k: state.get(k, "") for k in (LATEST, LATEST_URL, PUBLISHED)}


def _due(state: dict[str, str], now: datetime) -> bool:
    if state.get(URL_KEY, "") != config.RELEASE_CHECK_URL:
        return True
    last = state.get(LAST_AT, "")
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - then >= CHECK_EVERY


def newer_available(latest: str) -> bool:
    latest_v, running_v = parse_version(latest), parse_version(config.PINNED_OTODOCK_VERSION)
    return latest_v is not None and running_v is not None and latest_v > running_v


def notice() -> dict:
    """What the admin banner and the OtoDock tab show (sync; through run_db)."""
    state = _state()
    facts = _facts(state)
    on = enabled()
    latest = facts.get(LATEST, "")
    return {
        "enabled": on,
        "reason": disabled_reason(),
        "running": config.PINNED_OTODOCK_VERSION,
        "latest": latest if on else "",
        "url": facts.get(LATEST_URL, "") if on else "",
        "published_at": facts.get(PUBLISHED, "") if on else "",
        "checked_at": state.get(LAST_AT, "") if on else "",
        "error": state.get(ERROR, "") if on else "",
        "newer": on and newer_available(latest),
    }


async def _announce(facts: dict[str, str], state: dict[str, str]) -> None:
    latest = facts["version"]
    if state.get(NOTIFIED, "") == latest:
        return
    from services.notifications.notification_manager import fire_notification
    body = (f"This install runs OtoDock {config.PINNED_OTODOCK_VERSION}.\n"
            f"Release notes: {facts['url']}\nHow to upgrade: {UPGRADE_GUIDE_URL}")
    deliveries = await fire_notification(
        title=f"OtoDock {latest} is available", body=body, severity="info",
        scope="admin", source=SOURCE, href="/admin/platform?tab=otodock",
    )
    # No admin yet (a fresh install before its setup): the next check tells them.
    if deliveries:
        await run_db(_store, {NOTIFIED: latest})


async def check_now(*, force: bool = False) -> dict:
    """One check: due by the 24-hour rule, or forced (the admin route, a 60 s
    cooldown). Returns ``notice()`` whatever happened."""
    global _last_run_monotonic
    async with _lock:
        if not enabled():
            return await run_db(notice)
        state = await run_db(_state)
        now = datetime.now(timezone.utc)
        if force:
            if _last_run_monotonic is not None and time.monotonic() - _last_run_monotonic < COOLDOWN_S:
                return await run_db(notice)
        elif not _due(state, now):
            return await run_db(notice)
        _last_run_monotonic = time.monotonic()
        url = config.RELEASE_CHECK_URL
        try:
            facts = await fetch_latest(url)
        except ReleaseCheckError as e:
            logger.warning("release check: %s (the next tick retries)", e)
            await run_db(_store, {ERROR: str(e)})
            return await run_db(notice)
        await run_db(_store, {
            LAST_AT: now.isoformat(), URL_KEY: url, LATEST: facts["version"],
            LATEST_URL: facts["url"], PUBLISHED: facts["published_at"], ERROR: "",
        })
        if newer_available(facts["version"]):
            logger.info("release check: OtoDock %s is available (this install runs %s)",
                        facts["version"], config.PINNED_OTODOCK_VERSION)
            try:
                await _announce(facts, state)
            except Exception:
                logger.exception("release check: the admin notification failed (continuing)")
        return await run_db(notice)


# --- the worker ---------------------------------------------------------------

def start_worker() -> asyncio.Task | None:
    """Spawn the hourly worker when the check is on. Idempotent."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    reason = disabled_reason()
    if reason:
        logger.info("Release check worker not started (%s)", reason)
        return None
    _worker_task = asyncio.create_task(_loop(), name="release-check-worker")
    logger.info("Release check worker started (first tick in %ds, then every %ds, "
                "a check every %dh)", FIRST_TICK_S, TICK_S, CHECK_EVERY // timedelta(hours=1))
    return _worker_task


async def stop_worker() -> None:
    global _worker_task
    if not _worker_task:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _worker_task
    _worker_task = None


async def _loop() -> None:
    await asyncio.sleep(FIRST_TICK_S)
    while True:
        try:
            await check_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("release check tick failed (continuing)")
        await asyncio.sleep(TICK_S)
