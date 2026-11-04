"""This install's endpoint at the OtoDock event relay (hosted event delivery).

The relay forwards a provider's events (Slack, Notion, Zoom, the hosted
GitHub App) to ``{DASHBOARD_PUBLIC_URL}/v1/webhooks/relay/{provider}``. This
module keeps that endpoint matching what the install uses:

* **One registration per change, under one lock.** ``sync_provider`` reads
  the provider's state (``provider_gone``, ``relay_kinds``) and registers it:
  switched off when nothing here uses the provider, else on with the event
  kinds the subscriptions want. Counting, the union and the call share the
  lock, so a delete's switch-off can never land after a create's register.
  ``sync_provider_soon`` is the coalesced background form (one run per
  provider plus one more when asked meanwhile; a ``rate_limited`` refusal
  runs again after a minute, never dropped).
* **The signed life.** Every register is signed with the stored forward
  secret (``X-OtoDock-Install-Signature``); the relay refuses an unsigned
  change once the install has signed. A fresh install's first register
  cannot be signed: it mints the secret, which is stored from any answer and
  followed at once by a signed register of every provider in use (it marks
  the install and lights the rows a staff reset left dark). A signed
  register the relay refuses falls back once to an unsigned rotation; a
  rotation is never signed.
* **Every hosted account's provider.** An install holding a hosted
  (``via_relay``) account of a provider registers that provider too, with
  the kinds its relay subscriptions want or none, so the relay can send its
  account-removed notice there (``relay_notices``); while such an account is
  held the provider is never switched off and never answered with the 410.
* **The unbind.** When a hosted account's token file goes, the exact
  ``(scope, user)`` pair the relay routes to it is unbound, unless another
  account on this install still holds the pair. The unbind is queued in the
  platform settings and sent by one background sender, which stops at the
  first answer about the install rather than the item (a busy or
  unreachable relay runs again a minute later, a refused signature waits for
  the next secret).

Every call is best effort against the relay: an older relay ignores
``kinds`` and the signature and answers ``provider_unknown`` to an unbind.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import config
from services.billing import relay_client
from services.mcp import mcp_registry
from storage.automation import webhook_subscription_store
from storage.identity import credential_store, oauth_app_kinds
from storage.mcp import mcp_store

logger = logging.getLogger("otodock.relay-events")

# What the relay accepts as an event kind, and how many per provider.
_KIND_RE = re.compile(r"[a-z0-9_.:-]{1,64}")
_KINDS_MAX = 200
# A refused register or unbind (``rate_limited``: ten a minute per install)
# runs again after the relay's window.
RETRY_DELAY_S = 60.0
# A signed register refused again right after a fresh secret: the clock is
# off, and rotating on every sync would only spend secrets.
_ROTATE_BLOCK_S = 600.0
# A register the relay could not be reached for at boot, after a new secret
# or in the background is tried again this much later, then left for the
# next change or restart.
LATER_DELAYS_S = (300.0, 900.0)
# A refused signature (a clock off, a staff reset to come) is tried again
# this often until the relay takes it, never asking for a new secret while
# one is held; with none, the hourly register is the unsigned rotation a
# staff reset lets through.
HEAL_DELAY_S = 3600.0
_HEALED_BY_WAITING = ("bad_signature", "events_reset_needed")
_PENDING_UNBINDS = "relay_events_pending_unbinds"
_PENDING_UNBINDS_MAX = 500


@dataclass
class _LoopState:
    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    dirty: set[str] = field(default_factory=set)
    # The pending unbinds: their store, and the one sender that works them
    # through (never under ``lock``: an unbind's call never makes a create
    # or a register wait).
    pending_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sender: asyncio.Task[Any] | None = None
    sender_again: bool = False
    # The providers waiting on a later try or on the hourly heal.
    later: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    healing: dict[str, asyncio.Task[Any]] = field(default_factory=dict)


_state: _LoopState | None = None
# Until when a refused signature asks for no new secret (process-wide).
_rotate_blocked_until = 0.0


def _now() -> _LoopState:
    """The state of the running loop (the test suite runs several loops in
    one process, and a lock with waiters is bound to one)."""
    global _state
    loop = asyncio.get_running_loop()
    if _state is None or _state.loop is not loop:
        _state = _LoopState(loop=loop)
    return _state


def registration_lock() -> asyncio.Lock:
    """The one lock every relay registration of this process runs under."""
    return _now().lock


def events_url(provider_id: str) -> str:
    return f"{(config.DASHBOARD_PUBLIC_URL or '').rstrip('/')}/v1/webhooks/relay/{provider_id}"


def held_secret() -> str:
    return str((credential_store.get_infra_credentials(
        relay_client.EVENTS_FORWARD_SECRET_SLUG) or {}).get(relay_client.EVENTS_FORWARD_SECRET_KEY) or "")


def gone_mark(secret: str, provider_id: str, timestamp: str) -> str:
    """The mark only this install can put on a 410: the relay switches the
    provider's endpoint off for it, and a reverse proxy's bare 410 has none."""
    return "v0=" + hmac.new(
        secret.encode(), f"v0:gone:{provider_id}:{timestamp}".encode(), hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# What the install uses (sync: run in a thread)
# ---------------------------------------------------------------------------

def _webhooks_block(manifest: Any) -> dict:
    return (manifest.credentials.webhooks or {}) if manifest is not None and manifest.credentials else {}


def manifest_providers(manifest: Any) -> set[str]:
    out = {str(_webhooks_block(manifest).get("provider_id") or "")}
    oauth = (manifest.credentials.oauth or {}) if manifest.credentials else {}
    out.add(str(oauth.get("provider_id") or ""))
    out.discard("")
    return out


def _hosted_app_account_keeps(provider_id: str, *, leaving_user_sub: str = "") -> bool:
    """A hosted GitHub-App account needs the forwarding with no subscription
    at all: its installations' changes reach its card through it. An account
    whose MCP has no manifest loaded right now (an update in flight) keeps
    the provider too: gone is said only when certain. A person being
    deleted (``leaving_user_sub``) keeps nothing: their rows go next."""
    mcps = (credential_store.hosted_app_account_mcps(leaving_user_sub) if leaving_user_sub
            else credential_store.hosted_app_account_mcps())
    for mcp_name in mcps:
        manifest = mcp_registry.get_manifest(mcp_name)
        if manifest is None:
            # A workspace provider (Slack, Notion, Zoom) never comes from a
            # GitHub App: an unloaded manifest keeps every other one.
            if not _is_workspace_provider(provider_id):
                return True
        elif provider_id in manifest_providers(manifest):
            return True
    return False


def _is_workspace_provider(provider_id: str) -> bool:
    return any(
        _webhooks_block(m).get("provider_id") == provider_id and _webhooks_block(m).get("workspace_id_path")
        for m in mcp_registry.get_all_manifests().values()
    )


def _is_app_webhook_provider(provider_id: str) -> bool:
    return any(
        _webhooks_block(m).get("provider_id") == provider_id and _webhooks_block(m).get("app_webhook")
        for m in mcp_registry.get_all_manifests().values()
    )


def hosted_files(provider_id: str):
    """The person's token files of ``provider_id`` the relay issued."""
    from services.oauth import oauth_account_store
    for path in oauth_account_store.other_account_files(provider_id):
        extra = (oauth_account_store._read_oauth_token(path) or {}).get("extra")
        if isinstance(extra, dict) and extra.get("via_relay"):
            yield path, extra


def holds_hosted_account(provider_id: str) -> bool:
    """Whether a person here holds a hosted account of ``provider_id``."""
    return next(hosted_files(provider_id), None) is not None


def hosted_account_providers() -> set[str]:
    """Every provider a person here holds a hosted account of."""
    suffix = "-tokens"
    names = (p.name[: -len(suffix)] for p in config.SESSIONS_DIR.glob(f"*{suffix}") if p.is_dir())
    return {n for n in names if n and holds_hosted_account(n)}


def provider_gone(provider_id: str) -> bool:
    """Nothing on this install takes ``provider_id``'s relayed events or
    notices: no relay subscription of it in any status (a switched-off MCP's
    rows still count, a switch is undone), no hosted account of it and no
    hosted GitHub-App account. The one predicate behind the switch-off and
    the receiver's 410."""
    if webhook_subscription_store.list_subscriptions(provider_id=provider_id, delivery_mode="relay"):
        return False
    if holds_hosted_account(provider_id):
        return False
    return not _hosted_app_account_keeps(provider_id)


def _selected(row: dict) -> list[str]:
    value = row.get("selected_events")
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError:
            value = []
    return [str(k) for k in value] if isinstance(value, list) else []


def relay_kinds(provider_id: str) -> list[str] | None:
    """The event kinds the relay forwards for ``provider_id`` (None = every
    kind): the union of the relay subscriptions' keys whose MCP is installed
    and on, each mapped to the name the relay sees by one rule (the catalog
    entry's ``match.event_type`` when it has one, else the key itself:
    Slack's two message keys are ``message``). The install still filters on
    the rest. A GitHub-App provider adds the lifecycle kinds its accounts
    read. A name the relay would refuse widens to every kind."""
    from services.oauth import github_app_lifecycle

    states = mcp_store.get_all_mcp_states()
    kinds: set[str] = set()
    for row in webhook_subscription_store.list_subscriptions(provider_id=provider_id, delivery_mode="relay"):
        name = str(row.get("mcp_name") or "")
        manifest = mcp_registry.get_manifest(name)
        if manifest is None or states.get(name, True) is False:
            continue
        selected = _selected(row)
        if not selected:
            return None                     # the gate takes every event
        catalog = {e.get("key"): e for e in _webhooks_block(manifest).get("event_catalog") or []
                   if isinstance(e, dict)}
        for key in selected:
            match = (catalog.get(key) or {}).get("match")
            kinds.add(str(match.get("event_type") or key) if isinstance(match, dict) else key)
    if _is_app_webhook_provider(provider_id) or _hosted_app_account_keeps(provider_id):
        kinds |= {github_app_lifecycle.EVENT_INSTALLATION, github_app_lifecycle.EVENT_REPOSITORIES,
                  github_app_lifecycle.EVENT_TARGET, github_app_lifecycle.EVENT_AUTHORIZATION}
    refused = sorted(k for k in kinds if not _KIND_RE.fullmatch(k))
    if refused or len(kinds) > _KINDS_MAX:
        logger.warning("relay events: %s asks for every kind (%d kinds, refused names %s)",
                       provider_id, len(kinds), refused[:5])
        return None
    return sorted(kinds)


def providers_in_use() -> list[str]:
    """Every provider this install takes relayed events or notices of."""
    out = {str(r.get("provider_id") or "")
           for r in webhook_subscription_store.list_subscriptions(delivery_mode="relay")}
    out |= hosted_account_providers()
    for mcp_name in credential_store.hosted_app_account_mcps():
        manifest = mcp_registry.get_manifest(mcp_name)
        if manifest is not None:
            out |= {p for p in manifest_providers(manifest) if _is_app_webhook_provider(p)}
    out.discard("")
    return sorted(out)


# ---------------------------------------------------------------------------
# The registration, under the lock
# ---------------------------------------------------------------------------

def _store_secret(secret: str) -> None:
    credential_store.set_infra_credentials(
        relay_client.EVENTS_FORWARD_SECRET_SLUG, {relay_client.EVENTS_FORWARD_SECRET_KEY: secret})


async def _keep(out: dict | None, previous: str) -> bool:
    """Store a secret the relay minted (it answers one after a staff reset
    to whichever register switches events on next); whether one was."""
    fresh = str((out or {}).get("forward_secret") or "")
    if not fresh or fresh == previous:
        return False
    await asyncio.to_thread(_store_secret, fresh)
    from services.webhooks import webhook_dispatcher
    webhook_dispatcher.forget_forward_secret()
    logger.info("relay events: a new forward secret is stored")
    return True


async def _register_locked(provider_id: str, *, enabled: bool,
                           kinds: list[str] | None, may_rotate: bool) -> bool:
    """One register through the signed ladder; whether a minted secret was
    stored. Raises ``RelayError``: ``bad_signature`` for a refused signed
    call (the clock, or another secret), ``events_reset_needed`` for a
    refused unsigned one (the install signed before and lost its secret)."""
    url = events_url(provider_id)
    secret = await asyncio.to_thread(held_secret)
    if secret:
        try:
            out = await relay_client.events_register(
                provider_id=provider_id, events_url=url, enabled=enabled, kinds=kinds,
                sign_with=secret,
            )
            return await _keep(out, secret)
        except relay_client.RelayError as e:
            if e.code != "bad_signature" or not may_rotate or time.monotonic() < _rotate_blocked_until:
                raise
            logger.warning("relay events: the relay refused this install's signature for %s; "
                           "asking once for a new secret", provider_id)
    try:
        out = await relay_client.events_register(
            provider_id=provider_id, events_url=url, enabled=enabled, kinds=kinds,
            # An enabling register with no secret mints one; a switch-off
            # with none needs none.
            rotate_secret=enabled or bool(secret),
        )
    except relay_client.RelayError as e:
        if e.code == "bad_signature":
            raise relay_client.RelayError("events_reset_needed") from e
        raise
    return await _keep(out, secret)


async def _sync_locked(st: _LoopState, provider_id: str, done: set[str], *,
                       follow_up: bool = False, rotate: bool = True) -> None:
    gone = await asyncio.to_thread(provider_gone, provider_id)
    kinds = None if gone else await asyncio.to_thread(relay_kinds, provider_id)
    minted = await _register_locked(provider_id, enabled=not gone, kinds=kinds,
                                    may_rotate=rotate and not follow_up)
    done.add(provider_id)
    if minted and not follow_up:
        await _after_mint(st, provider_id, done)


async def _after_mint(st: _LoopState, provider_id: str, done: set[str]) -> None:
    """A secret was just minted: one signed register of the same provider
    (the relay marks the install at its first signed request), then every
    other provider in use (a staff reset clears every provider's row and a
    register fills only its own), then the unbinds that waited for a secret."""
    global _rotate_blocked_until
    others = [p for p in await asyncio.to_thread(providers_in_use) if p != provider_id and p not in done]
    todo = [provider_id, *others]
    for i, p in enumerate(todo):
        try:
            await _sync_locked(st, p, done, follow_up=True)
        except relay_client.RelayError as e:
            if e.code == "bad_signature":
                _rotate_blocked_until = time.monotonic() + _ROTATE_BLOCK_S
                logger.warning("relay events: the relay refused the first signed register after a new "
                               "secret: check this machine's clock (%s)", relay_client.relay_error_message(e.code))
                for q in todo[i:]:
                    _heal_later(st, q)
                return
            _after_refusal(st, p, e)
        except Exception as e:  # noqa: BLE001  # one provider's failure must not stop the others
            logger.info("relay events: the register of %s after a new secret failed: %s", p, e)
            _try_later(st, p)
    send_unbinds_soon()


async def sync_provider_locked(provider_id: str) -> None:
    """``sync_provider`` for a caller already holding ``registration_lock()``
    (a create that inserted its row under it)."""
    await _sync_locked(_now(), provider_id, set())


async def sync_provider(provider_id: str, *, rotate: bool = True) -> None:
    """Register ``provider_id``'s endpoint as the install's state says.
    Raises ``RelayError`` (the create shows it). ``rotate=False``: a refused
    signature never asks for a new secret (the hourly heal)."""
    st = _now()
    async with st.lock:
        await _sync_locked(st, provider_id, set(), rotate=rotate)


async def sync_all() -> None:
    """One register per provider in use (the boot's: an upgraded install
    sends its kinds, a mistaken switch-off is healed), then the unbinds
    waiting. Best effort."""
    if not relay_client.is_available() or not config.DASHBOARD_PUBLIC_URL:
        return
    st = _now()
    async with st.lock:
        done: set[str] = set()
        for p in await asyncio.to_thread(providers_in_use):
            if p in done:
                continue
            try:
                await _sync_locked(st, p, done)
            except relay_client.RelayError as e:
                _after_refusal(st, p, e)
            except Exception as e:  # noqa: BLE001  # the boot's register is best effort, per provider
                logger.info("relay events: the register of %s failed: %s", p, e)
                _try_later(st, p)
    if await asyncio.to_thread(_load_pending):
        send_unbinds_soon()


def _log_refusal(provider_id: str, e: relay_client.RelayError) -> None:
    loud = e.code in ("bad_signature", "events_reset_needed", "events_host_changed", "bad_events_url")
    logger.log(logging.WARNING if loud else logging.INFO,
               "relay events: the relay refused the register of %s: %s (%s)",
               provider_id, e.code, relay_client.relay_error_message(e.code))


def _after_refusal(st: _LoopState, provider_id: str, e: relay_client.RelayError) -> None:
    """A register refused outside a request: a busy relay goes to the
    coalesced retry, a refused signature to the hourly heal, the rest is
    logged."""
    if e.code == relay_client.RATE_LIMITED:
        sync_provider_soon(provider_id)
        return
    _log_refusal(provider_id, e)
    if e.code in _HEALED_BY_WAITING:
        _heal_later(st, provider_id)


def _try_later(st: _LoopState, provider_id: str) -> None:
    """The relay could not be reached: ``LATER_DELAYS_S`` later tries."""
    running = st.later.get(provider_id)
    if running is not None and not running.done():
        return
    from core import background
    st.later[provider_id] = background.spawn(
        _later(st, provider_id), name=f"later-{provider_id}", owner="relay-events")


async def _later(st: _LoopState, provider_id: str) -> None:
    try:
        for delay in LATER_DELAYS_S:
            await asyncio.sleep(delay)
            try:
                await sync_provider(provider_id)
                return
            except relay_client.RelayError as e:
                if e.code != relay_client.RATE_LIMITED:
                    _after_refusal(st, provider_id, e)
                    return
            except Exception as e:  # noqa: BLE001  # still unreachable: the next try, or the next change
                logger.info("relay events: the register of %s failed again: %s", provider_id, e)
        logger.warning("relay events: the register of %s did not reach the relay; it is sent again "
                       "at the next change of its subscriptions or the next restart", provider_id)
    finally:
        st.later.pop(provider_id, None)


def _heal_later(st: _LoopState, provider_id: str) -> None:
    """A refused signature heals by itself once the clock is right or the
    staff reset is done: try again every ``HEAL_DELAY_S``, signed and never
    asking for a new secret while one is held (after the reset the relay
    mints at that register); with none, the hourly register is the unsigned
    rotation a staff reset lets through."""
    running = st.healing.get(provider_id)
    if running is not None and not running.done():
        return
    from core import background
    st.healing[provider_id] = background.spawn(
        _heal(st, provider_id), name=f"heal-{provider_id}", owner="relay-events")


async def _heal(st: _LoopState, provider_id: str) -> None:
    try:
        while True:
            await asyncio.sleep(HEAL_DELAY_S)
            try:
                await sync_provider(provider_id, rotate=False)
            except relay_client.RelayError as e:
                if e.code in _HEALED_BY_WAITING or e.code == relay_client.RATE_LIMITED:
                    continue
                _log_refusal(provider_id, e)
                return
            except Exception as e:  # noqa: BLE001  # unreachable this hour: the next one
                logger.info("relay events: the hourly register of %s failed: %s", provider_id, e)
                continue
            logger.info("relay events: the relay takes this install's register of %s again", provider_id)
            return
    finally:
        st.healing.pop(provider_id, None)


def sync_provider_soon(provider_id: str) -> asyncio.Task[Any] | None:
    """``sync_provider`` in the background, coalesced: one run per provider
    in flight, plus one more when asked meanwhile (it reads the state at that
    moment). A ``rate_limited`` refusal runs again after ``RETRY_DELAY_S``.
    The task that will carry this request, None when there is no relay."""
    if not provider_id or not relay_client.is_available() or not config.DASHBOARD_PUBLIC_URL:
        return None
    st = _now()
    running = st.running.get(provider_id)
    if running is not None:
        st.dirty.add(provider_id)
        return running
    from core import background
    task = background.spawn(_run_soon(st, provider_id), name=f"sync-{provider_id}", owner="relay-events")
    st.running[provider_id] = task
    return task


def sync_all_soon() -> None:
    """Every provider in use, each through ``sync_provider_soon``."""
    if not relay_client.is_available() or not config.DASHBOARD_PUBLIC_URL:
        return
    from core import background

    async def run() -> None:
        for p in await asyncio.to_thread(providers_in_use):
            sync_provider_soon(p)
    background.spawn(run(), name="sync-all", owner="relay-events")


def sync_providers_of_mcp_soon(mcp_name: str) -> None:
    """After an MCP is switched on or off: the providers its manifest names
    that this install uses (``providers_in_use``). A provider nothing here
    uses is never registered: a switch alone must not reach the relay."""
    if not relay_client.is_available() or not config.DASHBOARD_PUBLIC_URL:
        return
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None:
        return
    named = manifest_providers(manifest)
    from core import background

    async def run() -> None:
        for p in sorted(named & set(await asyncio.to_thread(providers_in_use))):
            sync_provider_soon(p)
    background.spawn(run(), name=f"sync-{mcp_name}", owner="relay-events")


async def _run_soon(st: _LoopState, provider_id: str) -> None:
    try:
        while True:
            st.dirty.discard(provider_id)
            try:
                await sync_provider(provider_id)
            except relay_client.RelayError as e:
                if e.code == relay_client.RATE_LIMITED:
                    await asyncio.sleep(RETRY_DELAY_S)
                    st.dirty.add(provider_id)
                else:
                    _after_refusal(st, provider_id, e)
            except Exception as e:  # noqa: BLE001  # the relay's registration is best effort
                logger.info("relay events: the register of %s failed: %s", provider_id, e)
                _try_later(st, provider_id)
            if provider_id not in st.dirty:
                return
    finally:
        st.running.pop(provider_id, None)


# ---------------------------------------------------------------------------
# The unbind
# ---------------------------------------------------------------------------

def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _slack_pair(extra: dict) -> tuple[str, str] | None:
    team, authed = _dict(extra.get("team")), _dict(extra.get("authed_user"))
    return _pair(team.get("id") or extra.get("team_id"), authed.get("id") or extra.get("user_id"))


def _notion_pair(extra: dict) -> tuple[str, str] | None:
    user = _dict(_dict(extra.get("owner")).get("user"))
    return _pair(extra.get("workspace_id"), user.get("id") or extra.get("bot_id"))


def _zoom_pair(extra: dict) -> tuple[str, str] | None:
    return _pair(extra.get("account_id"), extra.get("id"))


def _pair(scope_id: Any, user_key: Any) -> tuple[str, str] | None:
    s, u = str(scope_id or ""), str(user_key or "")
    return (s, u) if s and u else None


# An account connected before the exchange named its binding: the relay's
# own reading of the same token response, which the token file keeps.
_DERIVED_PAIRS = {"slack": _slack_pair, "notion": _notion_pair, "zoom": _zoom_pair}


def binding_pair(provider_id: str, extra: dict) -> tuple[str, str] | None:
    """The ``(scope_id, user_key)`` the relay routes to this account, or
    None when it cannot be known."""
    bound = extra.get("relay_binding")
    if isinstance(bound, dict):
        pair = _pair(bound.get("scope_id"), bound.get("user_key"))
        if pair:
            return pair
    derive = _DERIVED_PAIRS.get(provider_id)
    return derive(extra) if derive else None


def _pair_held_elsewhere(provider_id: str, pair: tuple[str, str]) -> bool:
    """Whether another hosted account's token file on this install carries
    the same pair (a person's second label, Notion's bot consent): the relay
    cannot tell them apart, so the route stays."""
    from services.oauth import oauth_account_store
    for path in oauth_account_store.other_account_files(provider_id):
        extra = (oauth_account_store._read_oauth_token(path) or {}).get("extra") or {}
        if isinstance(extra, dict) and extra.get("via_relay") and binding_pair(provider_id, extra) == pair:
            return True
    return False


def _load_pending() -> list[dict]:
    from storage import database as db
    try:
        items = json.loads(db.get_platform_setting(_PENDING_UNBINDS) or "[]")
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("provider_id")] if isinstance(items, list) else []


def _save_pending(items: list[dict]) -> None:
    from storage import database as db
    if len(items) > _PENDING_UNBINDS_MAX:
        logger.warning("relay events: %d pending unbinds over the cap of %d dropped, the oldest first",
                       len(items) - _PENDING_UNBINDS_MAX, _PENDING_UNBINDS_MAX)
    db.set_platform_setting(_PENDING_UNBINDS, json.dumps(items[-_PENDING_UNBINDS_MAX:]))


def _key(item: dict) -> tuple[str, str, str]:
    return (str(item.get("provider_id") or ""), str(item.get("scope_id") or ""),
            str(item.get("user_key") or ""))


def _hint(item: dict) -> str:
    """The pair as a log line may carry it (never the user key in clear)."""
    p, s, u = _key(item)
    return f"{p} scope={s or '*'} user={hashlib.sha256(u.encode()).hexdigest()[:8] if u else '*'}"


# What one unbind came to: sent, or never to be sent (settled); or an answer
# about the install, not the item, which ends the pass with the rest kept:
# a busy or unreachable relay runs again after ``RETRY_DELAY_S``, no secret
# or a refused signature waits for the next secret, the boot or an unbind.
_UNBIND_SETTLED, _UNBIND_RETRY, _UNBIND_WAIT = "settled", "retry", "wait"


async def _unbind_one(item: dict) -> str:
    """Send one unbind (no lock held through the call)."""
    secret = await asyncio.to_thread(held_secret)
    if not secret:
        return _UNBIND_WAIT
    provider_id, scope_id, user_key = _key(item)
    try:
        out = await relay_client.events_unbind(
            provider_id=provider_id, scope_id=scope_id, user_key=user_key, sign_with=secret)
    except relay_client.RelayError as e:
        if e.code == relay_client.RATE_LIMITED:
            return _UNBIND_RETRY
        if e.code == "bad_signature":
            logger.info("relay events: unbind refused, the signature (%s)", _hint(item))
            return _UNBIND_WAIT
        if e.code == "provider_unknown":
            logger.info("relay events: the relay has no unbind yet (%s)", _hint(item))
            return _UNBIND_SETTLED
        # Any other refusal (an invalid unbind, a provider the relay does not
        # serve, the license) is final: sending it again changes nothing.
        logger.warning("relay events: unbind refused for good (%s): %s", _hint(item), e.code)
        return _UNBIND_SETTLED
    except Exception as e:  # noqa: BLE001  # an unreachable relay: the unbind waits for the next try
        logger.info("relay events: unbind failed (%s): %s", _hint(item), e)
        return _UNBIND_RETRY
    logger.info("relay events: unbound (%s), %s route(s)", _hint(item), (out or {}).get("removed", 0))
    return _UNBIND_SETTLED


async def _drop_pending(st: _LoopState, items: list[dict]) -> None:
    keys = {_key(i) for i in items}
    async with st.pending_lock:
        left = [i for i in await asyncio.to_thread(_load_pending) if _key(i) not in keys]
        await asyncio.to_thread(_save_pending, left)


async def forget_unbind(provider_id: str, pair: tuple[str, str]) -> None:
    """Drop a pending unbind of ``pair`` (the relay already ended it)."""
    await _drop_pending(_now(), [{"provider_id": provider_id, "scope_id": pair[0], "user_key": pair[1]}])


async def _send_pending(st: _LoopState) -> str:
    """One pass over the pending unbinds, in order, until an answer about
    the install; ``_UNBIND_SETTLED`` when every one went."""
    async with st.pending_lock:
        pending = await asyncio.to_thread(_load_pending)
    for item in pending:
        outcome = await _unbind_one(item)
        if outcome != _UNBIND_SETTLED:
            return outcome
        await _drop_pending(st, [item])
    return _UNBIND_SETTLED


def send_unbinds_soon() -> None:
    """Start the one background sender of the pending unbinds, or ask the
    running one for one more pass."""
    st = _now()
    if st.sender is not None and not st.sender.done():
        st.sender_again = True
        return
    from core import background
    st.sender = background.spawn(_sender(st), name="unbind", owner="relay-events")


async def _sender(st: _LoopState) -> None:
    while True:
        st.sender_again = False
        if await _send_pending(st) == _UNBIND_RETRY:
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        if not st.sender_again:
            return


async def unbind_account(provider_id: str, token_data: dict, *, revoked: bool,
                         leaving_user_sub: str = "") -> None:
    """The token file of a hosted account is gone: end the relay's route to
    it. The exact pair, never a scope alone (a Notion scope is shared by
    everyone on that workspace) and never the whole provider for one
    account. The hosted GitHub App's per-person route ends at the revoke;
    the provider as a whole is unbound only when the last hosted account
    went (``leaving_user_sub``: a person being deleted, whose rows still
    exist) and its revoke failed. Queued, sent in the background: the caller
    never waits on the relay. Best effort."""
    extra = _dict(token_data.get("extra"))
    if not provider_id or not extra.get("via_relay") or not relay_client.is_available():
        return
    try:
        if extra.get("app_kind") == oauth_app_kinds.KIND_GITHUB_APP:
            if revoked or await asyncio.to_thread(functools.partial(
                    _hosted_app_account_keeps, provider_id, leaving_user_sub=leaving_user_sub)):
                return
            item = {"provider_id": provider_id}
        else:
            pair = binding_pair(provider_id, extra)
            if pair is None:
                logger.info("relay events: no binding known for a %s account, nothing to unbind", provider_id)
                return
            item = {"provider_id": provider_id, "scope_id": pair[0], "user_key": pair[1]}
        st = _now()
        async with st.pending_lock:
            if "user_key" in item and await asyncio.to_thread(
                    _pair_held_elsewhere, provider_id, (item["scope_id"], item["user_key"])):
                logger.info("relay events: another account keeps the route (%s)", _hint(item))
                return
            pending = await asyncio.to_thread(_load_pending)
            if _key(item) not in {_key(i) for i in pending}:
                pending.append(item)
                await asyncio.to_thread(_save_pending, pending)
        send_unbinds_soon()
    except Exception:
        logger.exception("relay events: the unbind of a %s account failed", provider_id)
