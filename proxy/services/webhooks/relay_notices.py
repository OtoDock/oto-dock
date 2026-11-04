"""The relay's account-removed notice: a person removed OtoDock's app at a
vendor (Zoom's deauthorization, Meta's Deauthorize or Data Deletion, Slack's
``tokens_revoked`` or ``app_uninstalled``), the relay ended that account's
routes and tells this install at
``POST /v1/webhooks/relay/{provider}/account-removed``.

The relay's contract, as written for the install's side:

* **The signature first.** ``X-OtoDock-Notice-Signature`` is ``v0=`` and the
  hex HMAC-SHA256 of ``v0:notice:{timestamp}:{raw body}`` with the install's
  forward secret, ``X-OtoDock-Notice-Timestamp`` within 300 s. The ``notice:``
  word keeps it apart from a forward's (``v0:{digits}:``) and the 410 mark's
  (``v0:gone:``). A failure is 401; an ``install_id`` that is not this
  install's, a ``provider`` other than the path's or a malformed body is 400
  (the relay's final answers). Neither counts toward the per-address
  refusal limit: every notice comes from the relay's one address.
* **The accounts.** The hosted (``via_relay``) token files of the provider
  whose pair is the notice's ``(scope_id, user_key)``, read from
  ``extra.relay_binding`` when the file has it, else the pair the token
  response gives (``relay_events.binding_pair``: Slack, Zoom; a facebook
  file matches only through ``relay_binding``), and of those the ones bound
  before ``removed_at`` or with no ``bound_at`` (bound before it existed). A
  file connected again after the removal keeps its grant.
* **What goes.** Each file is deleted under the person's provider lock and
  its label lock (the order a connect and a disconnect take), a pending
  unbind of its pair is dropped (the relay already ended the route), the
  accounts that name it on this install are marked removed at the vendor
  (their card says "Reconnect needed" with that reason) and the agent
  choices that named them end, as a disconnect ends them. No revoke: the
  vendor ended the grant.
* **The answer.** ``200 {"ok": true, "removed": n}`` within 10 s: the
  deletions and the marks first, the answer, then what reaches the network
  (the subscriptions' vendor cleanup, the person's notice, the paired
  machines). The same ``notice_id`` again removes nothing more (a bounded
  record in the platform settings). A notice is never answered with the
  signed 410. An install older than this route answers the relay with the
  framework's 405 (its dashboard serves every other path for GET only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.billing import relay_client
from services.webhooks import relay_events

logger = logging.getLogger("otodock.relay-notices")

NOTICE_TYPE = "account_removed"
# The account row's mark and the reason its card shows.
REMOVED_REASON = "removed_at_vendor"
INTEGRATIONS_PATH = "/user-settings/integrations"

_WINDOW_S = 300
_TIMESTAMP_HEADER = "x-otodock-notice-timestamp"
_SIG_BLOCK = {
    "algorithm": "hmac-sha256",
    "header": "x-otodock-notice-signature",
    "version_prefix": "v0=",
    "timestamp_header": _TIMESTAMP_HEADER,
    "timestamp_format": "unix",
    "signed_payload_template": "v0:notice:{timestamp}:{body}",
    "max_age_seconds": _WINDOW_S,
}
_NOTICE_ID_RE = re.compile(r"[0-9a-f]{32}")
_REASON_RE = re.compile(r"[a-z_]{1,64}")
_FIELD_MAX = 256
# A notice is under a kilobyte: the route reads at most this much, so a body
# that will not verify costs a small read and a small digest.
BODY_CAP = 16 * 1024
# The notices already acted on, newest last, at most this many.
_SEEN_SETTING = "relay_notices_seen"
_SEEN_MAX = 1000
# The relay waits 10 s for the answer: the local work is bounded below it,
# and a notice it could not finish is answered busy and sent again.
_LOCAL_DEADLINE_S = 8.0
_JSON = {"content-type": "application/json"}

_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        _locks.clear()
        lock = _locks[loop] = asyncio.Lock()
    return lock


@dataclass(frozen=True)
class Notice:
    notice_id: str
    provider_id: str
    scope_id: str
    user_key: str
    reason: str
    removed_at: int


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    return value if isinstance(value, str) and 0 < len(value) <= _FIELD_MAX else ""


def parse(raw_body: bytes) -> tuple[Notice | None, str]:
    """The notice the body carries, or why it is malformed."""
    try:
        body = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return None, "the body is not JSON"
    if not isinstance(body, dict):
        return None, "the body is not an object"
    if body.get("type") != NOTICE_TYPE:
        return None, "unknown notice type"
    notice_id = _text(body, "notice_id")
    if not _NOTICE_ID_RE.fullmatch(notice_id):
        return None, "bad notice_id"
    fields = {k: _text(body, k) for k in ("install_id", "provider", "scope_id", "user_key")}
    missing = sorted(k for k, v in fields.items() if not v)
    if missing:
        return None, f"missing {', '.join(missing)}"
    reason = _text(body, "reason")
    if not _REASON_RE.fullmatch(reason):
        return None, "bad reason"
    # Unix seconds, whole: JSON's Infinity and NaN, a fraction, a negative
    # number or a string never reach a file.
    removed_at = body.get("removed_at")
    if isinstance(removed_at, float) and math.isfinite(removed_at) and removed_at.is_integer():
        removed_at = int(removed_at)
    if isinstance(removed_at, bool) or not isinstance(removed_at, int) or removed_at <= 0:
        return None, "bad removed_at"
    return Notice(notice_id, fields["provider"], fields["scope_id"], fields["user_key"], reason,
                  removed_at), fields["install_id"]


def _after_receipt(notice: Notice, headers: dict[str, str]) -> bool:
    """A removal later than the relay's receipt (at most the window before
    the signed timestamp) is a relay fault: acted on, it would match a file
    connected after the real removal."""
    try:
        signed = float(headers.get(_TIMESTAMP_HEADER, ""))
    except ValueError:
        return True
    return not math.isfinite(signed) or notice.removed_at > signed + _WINDOW_S


async def _verify(raw_body: bytes, headers: dict[str, str]):
    """The notice signature with the held forward secret, read once more
    when the cached one fails (the relay may have rotated it)."""
    from auth.webhook_providers.generic import GenericWebhookProvider
    from services.webhooks import webhook_dispatcher as wd
    relay = GenericWebhookProvider(provider_id="relay")
    secret = await wd._forward_secret(max_age_s=wd._RELAY_SECRET_TTL_S)
    result = relay.verify_signature(raw_body=raw_body, headers=headers, signing_secret=secret,
                                    manifest_sig_block=_SIG_BLOCK)
    if not result.ok:
        fresh = await wd._forward_secret(max_age_s=wd._RELAY_SECRET_RETRY_S)
        if fresh != secret:
            result = relay.verify_signature(raw_body=raw_body, headers=headers, signing_secret=fresh,
                                            manifest_sig_block=_SIG_BLOCK)
    return result


# ---------------------------------------------------------------------------
# The accounts (sync: run in a thread)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Match:
    path: Path
    user_sub: str
    label: str


def _bound_before(extra: dict, removed_at: float) -> bool:
    """Bound before the removal, or with no ``bound_at`` (bound before the
    relay recorded it): a file connected again after it keeps its grant."""
    bound = extra.get("relay_binding")
    at = bound.get("bound_at") if isinstance(bound, dict) else None
    if at is None or isinstance(at, bool) or not isinstance(at, (int, float)):
        return True
    return at < removed_at


def matches(notice: Notice, extra: Any) -> bool:
    """Whether a token file's ``extra`` is a hosted account the notice
    names and still holds the grant the vendor ended."""
    if not isinstance(extra, dict) or not extra.get("via_relay"):
        return False
    if relay_events.binding_pair(notice.provider_id, extra) != (notice.scope_id, notice.user_key):
        return False
    return _bound_before(extra, notice.removed_at)


def _matching(notice: Notice) -> list[_Match]:
    from storage import database as task_store
    out = []
    for path, extra in relay_events.hosted_files(notice.provider_id):
        if not matches(notice, extra):
            continue
        user_sub = task_store.get_user_sub_by_username(path.parent.name)
        if user_sub:
            out.append(_Match(path, user_sub, path.stem))
    return out


def _still_matches(notice: Notice, path: Path) -> bool:
    from services.oauth import oauth_account_store
    return matches(notice, (oauth_account_store._read_oauth_token(path) or {}).get("extra"))


def _accounts_naming(user_sub: str, label: str, provider_id: str) -> list[str]:
    """The MCPs of the provider whose account row names the file's label."""
    from services.mcp import mcp_registry
    from storage.identity import credential_store
    mcps = {m.name for m in mcp_registry.get_mcps_by_provider(provider_id)}
    return sorted(mcps & set(credential_store.mcps_naming_label(user_sub, label)))


def _mark(match: _Match, mcp_names: list[str], notice: Notice) -> None:
    """The rows say the account was removed at the vendor; the agent
    choices that named it end, as a disconnect ends them."""
    from storage.identity import credential_store
    mark = {"reason": notice.reason, "at": int(notice.removed_at)}
    for mcp_name in mcp_names:
        credential_store.update_account_meta(
            match.user_sub, mcp_name, match.label, lambda meta: {**meta, REMOVED_REASON: mark})
        for row in credential_store.list_agent_account_bindings(match.user_sub, mcp_name):
            if row.get("account_label") == match.label:
                credential_store.remove_account_agent_binding(match.user_sub, mcp_name, row["agent_name"])


def clear_mark(user_sub: str, provider_id: str, label: str) -> None:
    """A sign-in wrote the label's file anew: no row that reads it says the
    account was removed at the vendor any more, the rows of the provider's
    other MCPs included (the sign-in writes only its own card's row). The
    rest of each row's meta stays."""
    from storage.identity import credential_store

    def change(meta: dict) -> dict | None:
        if REMOVED_REASON not in meta:
            return None
        return {k: v for k, v in meta.items() if k != REMOVED_REASON}
    for mcp_name in _accounts_naming(user_sub, label, provider_id):
        credential_store.update_account_meta(user_sub, mcp_name, label, change)


def removed_reason(user_sub: str, mcp_name: str, label: str) -> str:
    """``REMOVED_REASON`` while an account's row carries the mark (a new
    sign-in to the label, through any MCP of its provider, clears it:
    ``clear_mark``), else ``""``."""
    from storage.identity import credential_store
    meta = credential_store.get_account_meta(user_sub, mcp_name, label)
    return REMOVED_REASON if isinstance(meta.get(REMOVED_REASON), dict) else ""


def _seen(notice_id: str) -> bool:
    from storage import database as db
    try:
        ids = json.loads(db.get_platform_setting(_SEEN_SETTING) or "[]")
    except json.JSONDecodeError:
        return False
    return isinstance(ids, list) and notice_id in ids


def _remember(notice_id: str) -> None:
    from storage import database as db
    try:
        ids = json.loads(db.get_platform_setting(_SEEN_SETTING) or "[]")
    except json.JSONDecodeError:
        ids = []
    ids = [i for i in ids if isinstance(i, str) and i != notice_id] if isinstance(ids, list) else []
    ids.append(notice_id)
    db.set_platform_setting(_SEEN_SETTING, json.dumps(ids[-_SEEN_MAX:]))


# ---------------------------------------------------------------------------
# The receiver
# ---------------------------------------------------------------------------

def _remove_one(notice: Notice, match: _Match, done: list[tuple[_Match, list[str]]]) -> None:
    """One file, whole: re-read, deleted, its rows marked, then recorded in
    ``done`` (a thread finishes its file even when the pass is given up)."""
    from services.oauth import oauth_account_store
    if not _still_matches(notice, match.path):
        return
    oauth_account_store.delete_account_token(match.path.parent, match.label)
    mcp_names = _accounts_naming(match.user_sub, match.label, notice.provider_id)
    _mark(match, mcp_names, notice)
    done.append((match, mcp_names))


async def _remove(notice: Notice, done: list[tuple[_Match, list[str]]]) -> None:
    """Delete the matching files and mark their rows (local work only),
    each one recorded in ``done``, which the caller owns."""
    from core.credentials import credential_locks
    for match in await asyncio.to_thread(_matching, notice):
        async with credential_locks.get_lock(match.user_sub, notice.provider_id, ""), \
                credential_locks.get_lock(match.user_sub, notice.provider_id, match.label):
            await asyncio.to_thread(_remove_one, notice, match, done)
    if done:
        await relay_events.forget_unbind(notice.provider_id, (notice.scope_id, notice.user_key))


async def receive(provider_id: str, raw_body: bytes, headers: dict[str, str]) -> tuple[int, dict, dict]:
    """The notice route's answer (``api/events/webhooks.py``)."""
    headers_lc = {k.lower(): v for k, v in (headers or {}).items()}
    verify = await _verify(raw_body, headers_lc)
    if not verify.ok:
        return 401, {"error": "notice signature verification failed", "reason": verify.reason}, _JSON
    notice, install_or_reason = parse(raw_body)
    if notice is None:
        return 400, {"error": "malformed notice", "reason": install_or_reason}, _JSON
    if _after_receipt(notice, headers_lc):
        return 400, {"error": "malformed notice", "reason": "bad removed_at"}, _JSON
    if install_or_reason != await asyncio.to_thread(relay_client.get_install_id):
        return 400, {"error": "the notice is for another install"}, _JSON
    if notice.provider_id != provider_id:
        return 400, {"error": "provider mismatch"}, _JSON

    from core import background
    done: list[tuple[_Match, list[str]]] = []

    async def act() -> bool:
        async with _lock():
            if await asyncio.to_thread(_seen, notice.notice_id):
                return False
            await _remove(notice, done)
            await asyncio.to_thread(_remember, notice.notice_id)
            return True

    started = time.monotonic()
    try:
        acted = await asyncio.wait_for(act(), _LOCAL_DEADLINE_S)
    except asyncio.TimeoutError:
        logger.warning("relay notice %s for %s: not finished in time (%d account(s) removed so far), "
                       "the relay sends it again", notice.notice_id[:8], provider_id, len(done))
        # The files already gone keep their cleanup and their person's notice;
        # the relay's next try finds the rest.
        if done:
            background.spawn(_after(notice, list(done)), name=f"notice-{notice.notice_id[:8]}",
                             owner="relay-notices")
        return 503, {"error": "busy"}, _JSON
    if not acted:
        logger.info("relay notice %s for %s: already acted on", notice.notice_id[:8], provider_id)
        return 200, {"ok": True, "removed": 0}, _JSON
    logger.info("relay notice %s for %s (%s): %d account(s) removed in %.2fs", notice.notice_id[:8],
                provider_id, notice.reason, len(done), time.monotonic() - started)
    if done:
        background.spawn(_after(notice, list(done)), name=f"notice-{notice.notice_id[:8]}", owner="relay-notices")
    return 200, {"ok": True, "removed": len(done)}, _JSON


async def _after(notice: Notice, done: list[tuple[_Match, list[str]]]) -> None:
    """What reaches the network, after the answer: each account's
    subscriptions and the service bindings that lent it (as a disconnect
    clears them), the paired machines, the person's notice."""
    from core.remote import mcp_gateway_push
    from services.mcp import mcp_registry
    from services.notifications import notification_manager
    from services.webhooks import subscription_manager
    from storage.identity import credential_store
    for match, mcp_names in done:
        for mcp_name in mcp_names:
            try:
                await subscription_manager.cleanup_account_subscriptions(
                    scope="user", owner=match.user_sub, mcp_name=mcp_name, account_label=match.label)
                for b in await asyncio.to_thread(credential_store.list_service_agent_bindings, mcp_name):
                    if b.get("account_owner_sub") == match.user_sub and b.get("account_label") == match.label:
                        agent = str(b.get("agent_name") or "")
                        await subscription_manager.cleanup_account_subscriptions(
                            scope="service", owner=match.user_sub, mcp_name=mcp_name,
                            account_label=match.label, agent=agent)
                        await asyncio.to_thread(
                            credential_store.remove_service_agent_binding, mcp_name, agent,
                            owner_sub=match.user_sub, account_label=match.label)
            except Exception:
                logger.exception("relay notice: the cleanup of %s's subscriptions failed", mcp_name)
        labels = [getattr(mcp_registry.get_manifest(n), "label", n) for n in mcp_names]
        vendor = labels[0] if labels else notice.provider_id
        try:
            await notification_manager.fire_notification(
                title=f"{match.label} was removed at {vendor}",
                body=(f"OtoDock's app was removed from this account at {vendor}. Agents can no "
                      "longer use it. Reconnect it in User Settings, Integrations to use it again."),
                severity="warning", scope="user", target=match.user_sub, source="relay_notice",
                source_id=f"{notice.notice_id}:{match.label}", href=INTEGRATIONS_PATH,
            )
        except Exception:
            logger.exception("relay notice: the person's notice was not sent")
    mcp_gateway_push.tick_soon()
