"""The acquisition chokepoint and the scope rebalance. The scope's sticky pick, the acquisition and the
move of a scope group call each other, so they share one module.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import cast

import psycopg

from core.execution_layer import SubscriptionHandle
from services.billing import pool_caps as _pool_caps
from services.engines import pool_moves
from services.engines.pool_bindings import (
    _sticky_subscription_id,
    release_unbound_seat,
)
from services.engines.pool_moves import Cause
from services.engines.pool_select import (
    _cap_status,
    _out_for,
    _rows_without_login,
    _scope_facts,
    _ScopeFacts,
    _select,
)
from services.engines.pool_state import (
    _SCOPE_RECENT_TTL_S,
    _USER_BORROWABLE_AUTH_TYPES,
    _scope_recent,
    _session_binding_ctx,
    _session_bound_at,
    _session_maps_lock,
    _session_moved_at,
    _session_scope_keys,
    _session_subscriptions,
    _session_token_expiry,
)
from services.engines.pool_writer import (
    _queue_write,
    flush_binding_writes,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)


# A scope that returned stays put for the cooldown, so a cold chat is not sent
# back and forth as two accounts' resets come near. A move off a mark, a
# refusal and a delisting are never held back by it, and a pass that found no
# replacement stamps nothing. In-memory: a restart just re-arms one move.
_SCOPE_REBALANCE_COOLDOWN_S = 45 * 60


_scope_rebalance_last: dict[str, float] = {}  # scope_key → monotonic ts


class NoSubscriptionError(Exception):
    """Raised when a spawn resolves to no usable credentials, so the dashboard
    can show an actionable message instead of a cryptic provider 401.
    ``reason`` ∈ {auth_off, admin_oauth_only, no_pool, none, throttled,
    own_sub_expired} for user-scoped work (see ``user_scope_block_reason``),
    ``pool_cap`` for either scope when the pool's subscription cap refused
    the spawn (``message`` carries the cap's own wording), ``folder_busy``
    when the start would write its sign-in under live sessions on another
    account (``_crosses_live_sign_in``)."""

    _MESSAGES = {
        "throttled": "Your AI engine is busy right now, so try again in a few seconds.",
        "own_sub_expired": "Your AI engine's sign-in has expired, so reconnect it to keep using it.",
        "auth_off": "This runs on an AI engine you have not connected yet.",
        "admin_oauth_only": "This runs on an AI engine you have not connected yet.",
        "no_pool": "No AI engine is connected here yet, so connect your own or ask an admin to add one.",
        "none": "The AI engine could not be used just now, so try again or connect your own.",
        "pool_cap": "You have reached the limit on AI accounts in use. It clears when their "
                    "usage resets. An admin can change it on the Usage page.",
        "local_only": "This agent allows only local models on this engine. Ask an "
                      "administrator to add a local model server in Setup, AI "
                      "Engines, or pick another engine.",
        "folder_busy": "Other sessions of this agent are running on another AI account, and "
                       "this engine cannot change its sign-in while they run. It can start "
                       "once they end.",
    }

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or self._MESSAGES.get(reason, self._MESSAGES["none"]))


def _engine_capabilities(layer: str):
    from core.session.session_manager import UnknownExecutionPath, get_layer_by_path
    try:
        return get_layer_by_path(layer).capabilities
    except UnknownExecutionPath:
        return None


def _takes_sign_in_in_place(layer: str) -> bool:
    """Whether a live session of the engine takes another account's
    sign-in file (its ``AuthProfile.sign_in_in_place``). An engine that does
    not is never moved by a rewrite, and its scope's pin never yields while
    a session holds it unless the vendor refused the pinned account: a start
    on another account would write that account into the folder its live
    sessions read."""
    caps = _engine_capabilities(layer)
    return bool(caps is not None and caps.auth.sign_in_in_place)


def _crosses_live_sign_in(layer: str, sticky_scope: str, chosen: str, model: str) -> bool:
    """Whether a new start of an engine that cannot change its sign-in under
    a live process (``_takes_sign_in_in_place``) would write ``chosen`` into
    a folder committed to another account the vendor still serves: another
    payer's (a person's lane beside the agent's own work, or two people on a
    Shared-only agent). Such a start is refused, never written under them.
    The folder's account is the pin's (``_sticky_subscription_id``): its
    newest live session's, else a start in flight (its claim, until the
    layer binds), else, in the boot window, a stored binding of a session
    not yet taken back after a restart. An older neighbour still bound to
    another account waits on a refused one and already reads the newer
    file."""
    if not sticky_scope or _takes_sign_in_in_place(layer):
        return False
    held = _sticky_subscription_id(sticky_scope) or ""
    return bool(held) and held != chosen and not pool_moves.refused(held, layer, frozenset({model}))


def _cache_lifetime_s(layer: str) -> int:
    """The engine's declared prompt-cache lifetime (``behaviour.prompt_cache_s``), 0 when it declares none."""
    caps = _engine_capabilities(layer)
    return int(caps.behaviour.prompt_cache_s) if caps is not None else 0


def _select_sticky(
    sticky_scope: str,
    candidates: list[dict],
    *,
    allowed_auth: frozenset[str] | None,
    model: str = "",
) -> SubscriptionHandle | None:
    """Reuse the scope's committed account IF it is still in this acquisition's
    candidate list (same eligibility the normal path enforces — a delisted or
    non-borrowable pin falls through to fresh selection; the rebind fan-out
    re-homes the scope's live sessions in that case). Throttling is
    deliberately NOT honored here: the shared-file constraint dominates a
    briefly resting account (``rebalance_scopes`` moves the whole scope).
    Window EXHAUSTION for the spawn's model is, and so is a rest for the
    model's scope after a usage limit on that family's window: a pin the
    vendor will refuse is no pin. When another candidate can serve the
    model, the spawn goes there and a rebalance pass is scheduled so the
    scope's other sessions follow onto the same account (the new spawn's
    credential file already points them there; the pass formalizes the
    bindings). Live-observed
    2026-09-11: sessions kept spawning onto an account at 96 % of its
    session window for a tick while the other account had headroom. The
    pin of an engine that refuses another account's sign-in under a live
    process (``_takes_sign_in_in_place``) yields only to a refusal of the
    pinned account, never to a window past its mark."""
    if not sticky_scope:
        return None
    pinned = _sticky_subscription_id(sticky_scope)
    if not pinned:
        return None
    match = [c for c in candidates if c["id"] == pinned]
    if not match:
        return None
    pin_layer = str(match[0].get("layer") or "")
    if not _takes_sign_in_in_place(pin_layer):
        if pool_moves.refused(pinned, pin_layer, frozenset({model})):
            # The vendor refused the pinned account: the folder's live sessions
            # fail on it anyway and are closed when idle, so a start goes to
            # the pool's pick.
            logger.info("Pool: scope-sticky account %s is refused for %s — spawning on the pool's pick",
                        pinned[:8], model or "this spawn")
            return None
    elif _out_for(match[0], model):
        others = [
            c for c in candidates
            if c["id"] != pinned and (allowed_auth is None or c.get("auth_type") in allowed_auth)
            and not _out_for(c, model)
        ]
        if others:
            logger.info(
                "Pool: scope-sticky account %s is out of window for "
                "%s — spawning on the pool's pick and moving the scope",
                pinned[:8], model or 'this spawn'
            )
            schedule_rebalance("sticky account exhausted")
            return None
    handle = _select(match, allowed_auth=allowed_auth, model=model)
    if handle:
        logger.info(
            "Pool: scope-sticky reuse of subscription %s "
            "(scope already holds its credential file)", pinned[:8]
        )
    return handle


def acquire_subscription(
    layer: str,
    user_sub: str | None,
    *,
    provider: str = "",
    sticky_scope: str = "",
    model: str = "",
    enforce_caps: bool = True,
    allowed_auth: frozenset[str] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> SubscriptionHandle | None:
    """Select and acquire a subscription for a new session. Off the loop
    (``asyncio.to_thread``): the queued seat releases are flushed first so
    the counts it reads are current.

    USER-SCOPE (``user_sub`` truthy): the user's own accounts (``use_personal``)
    first; then — only if Platform Auth is on — the admin pool restricted to
    BORROWABLE API credentials (api_key / relay / local_endpoint; never an admin
    OAuth subscription).  AGENT-SCOPE (``user_sub`` None/''): the full platform
    pool, OAuth subscriptions included.

    ``sticky_scope`` (CLI layers only — see ``credential_scope_key``): when the
    scope already has a live/just-acquired/persisted binding to account X and X
    is still an eligible candidate, X is reused instead of the headroom pick —
    the scope's sessions share ONE credential file, and re-selection happens at
    every spawn/re-warm, so without stickiness two same-scope sessions could
    fight over the file with different accounts.

    ``model`` (the spawn's model id, when known) lets the selection honour a
    per-model weekly window: an account whose "Fable" window is full is
    skipped for a Fable spawn and still serves a Sonnet one.

    ``enforce_caps``: the pool's subscription cap (``services.billing.pool_caps``)
    gates NEW spawns only. On a hit, ``stop`` raises ``NoSubscriptionError
    ("pool_cap")`` for BOTH scopes (an agent-scope spawn that acquired nothing
    would otherwise start on whatever credential file its scope dir still
    holds); ``continue`` drops the OAuth accounts from the candidates so an
    API key, the relay or a local endpoint takes the spawn, and raises the
    same when none is there. The re-homing of LIVE sessions (the delisting
    rebind and the scope rebalance) passes ``False``: a cap never moves or
    strands a running session.

    ``allowed_auth``: a caller's narrowing of the rows BOTH branches may take
    (the local-only rule: an agent that allows only local models on the
    engine passes the local rows' type, ``resolve_subscription_env``); the
    pool branch keeps its borrowable filter underneath. When the narrowing
    leaves nothing, ``NoSubscriptionError("local_only")`` is raised for BOTH
    scopes, as a cap is: an agent-scope spawn that acquired nothing would
    otherwise start on whatever credential file its scope dir still holds.

    ``exclude``: accounts this acquisition never takes (a live session's
    move leaves out the account it leaves and, after a refusal, every account
    the vendor refuses for its models).

    Returns None when nothing is available (caller surfaces the block; see
    ``user_scope_block_reason`` for the user-facing reason).
    """
    flush_binding_writes()
    user_sub = user_sub or None  # treat "" as agent-scope; never match owner_sub='' infra
    borrowable = (_USER_BORROWABLE_AUTH_TYPES if allowed_auth is None
                  else _USER_BORROWABLE_AUTH_TYPES & allowed_auth)

    drop_oauth = False
    cap = _cap_status(layer, user_sub) if enforce_caps else None
    if cap is not None and not cap.allowed:
        scope_name = "user" if user_sub else "platform"
        if cap.on_reached != "continue":
            logger.info(
                "Pool: %s pool cap reached on layer=%s "
                "(%s) — spawn refused", scope_name, layer, cap.hit_text()
            )
            raise NoSubscriptionError("pool_cap", cap.blocked_message())
        logger.info(
            "Pool: %s pool cap reached on layer=%s "
            "(%s) — OAuth accounts excluded, continuing on a key",
            scope_name, layer, cap.hit_text()
        )
        drop_oauth = True

    handle: SubscriptionHandle | None = None
    if user_sub:
        # 1. The user's own usable accounts (any auth type, incl. their own OAuth)
        personal = [r for r in subscription_store.list_personal(layer, user_sub, provider or None)
                    if r.get("id") not in exclude]
        if drop_oauth:
            personal = _rows_without_login(personal)
        handle = _select_sticky(sticky_scope, personal, allowed_auth=allowed_auth, model=model) \
            or _select(personal, allowed_auth=allowed_auth, model=model)
        if not handle:
            # 2. Platform fallback, gated by the per-user Platform Auth toggle
            if not subscription_store.get_user_allow_platform_auth(user_sub):
                logger.info("Pool: user %s has platform auth disabled, no subscription available", user_sub[:8])
            else:
                # 3. Borrow ONLY admin API-type credentials — never an admin OAuth sub
                platform = [r for r in subscription_store.list_platform_pool(layer, provider or None)
                            if r.get("id") not in exclude]
                handle = _select_sticky(sticky_scope, platform,
                                        allowed_auth=borrowable, model=model) \
                    or _select(platform, allowed_auth=borrowable, model=model)
                if handle:
                    # Defense-in-depth: a user-scope handle must never be an OAuth subscription.
                    assert handle.auth_type in _USER_BORROWABLE_AUTH_TYPES, (
                        f"user-scope acquired non-borrowable auth_type={handle.auth_type}"
                    )
                else:
                    logger.warning("Pool: no borrowable platform credentials for user %s, layer=%s", user_sub[:8], layer)
    else:
        # AGENT-SCOPE: the full platform pool (OAuth subscriptions allowed)
        platform = [r for r in subscription_store.list_platform_pool(layer, provider or None)
                    if r.get("id") not in exclude]
        if drop_oauth:
            platform = _rows_without_login(platform)
        handle = _select_sticky(sticky_scope, platform, allowed_auth=allowed_auth, model=model) \
            or _select(platform, allowed_auth=allowed_auth, model=model)

    if handle is None:
        if allowed_auth is not None:
            logger.info("Pool: no row of %s on layer=%s for the %s scope: the agent allows "
                        "only local models there", sorted(allowed_auth), layer,
                        "user" if user_sub else "platform")
            raise NoSubscriptionError("local_only")
        if drop_oauth:
            # ``continue`` with nothing to continue on: say so, rather than
            # the generic "connect an account" classification (only a reached
            # cap sets drop_oauth, above).
            raise NoSubscriptionError("pool_cap", cast("_pool_caps.CapStatus", cap).blocked_message(no_key=True))
        return None

    if enforce_caps and _crosses_live_sign_in(layer, sticky_scope, handle.subscription_id, model):
        release_unbound_seat(handle.subscription_id)
        logger.warning("Pool: a start on %s for %s would write account %s into folder %r under "
                       "live sessions on another account: refused", layer,
                       (user_sub or "the platform")[:8], handle.subscription_id[:8], sticky_scope)
        raise NoSubscriptionError("folder_busy")

    if sticky_scope:
        # Stickiness can only bridge sessions whose candidate lists overlap. A
        # Shared-only agent's chats share ONE credential dir across ALL users
        # (sender-pays: each user's session runs on their own account), so two
        # users concurrently active on such an agent land different accounts on
        # the same file — the last-write-wins flap stickiness exists to prevent.
        # Surface it loudly; the full fix is per-payer credential delivery
        # for shared scopes. Under a cap's ``continue`` the switch to a key
        # is the point, so it is only noted.
        held = _sticky_subscription_id(sticky_scope)
        if held and held != handle.subscription_id:
            (logger.info if drop_oauth else logger.warning)(
                f"Pool: credential scope {sticky_scope!r} is live on subscription "
                f"{held[:8]} but this spawn selected {handle.subscription_id[:8]} "
                f"({'pool cap, continuing on a key' if drop_oauth else 'different payer'})"
                f" — concurrent sessions on this scope may contend over the "
                f"shared credential file"
            )
        # Commit the scope to this account for the acquire→bind window, so a
        # concurrent same-scope spawn can't pick a different one meanwhile.
        with _session_maps_lock:
            _scope_recent[sticky_scope] = (handle.subscription_id, time.time())
    return handle


# Serialize rebind passes (endpoint hook vs freshness tick): concurrent passes
# would double-acquire replacements for the same groups. Passes are quick, so
# the second caller just waits its turn.
_rebind_lock = threading.Lock()


# Keep fire-and-forget rebind tasks referenced until done (an unreferenced
# asyncio.Task can be garbage-collected mid-flight).
_rebind_tasks: set[asyncio.Task] = set()


def schedule_rebalance(reason: str) -> None:
    """Fire-and-forget a ``rebalance_scopes`` pass — called by
    ``mark_subscription_throttled`` the moment a REAL provider limit lands, so
    every scope pinned to the limited account fails over within moments
    instead of erroring until the next freshness tick. Unlike
    ``schedule_rebind`` this must also work from WORKER THREADS: the
    interactive tailers (the main limit reporters) run via ``to_thread``, so
    with no running loop it hops to the loop token_fanout captured at startup.
    The worker's per-tick pass is the retry loop, so a lost task (no loop at
    all: tests/scripts) only delays convergence by ≤5 min."""
    def _spawn(loop: asyncio.AbstractEventLoop) -> None:
        task = loop.create_task(
            asyncio.to_thread(rebalance_scopes, reason=reason)
        )
        _rebind_tasks.add(task)
        task.add_done_callback(_rebind_tasks.discard)

    from services.engines import token_fanout
    # No loop at all (tests / scripts): the worker pass converges.
    token_fanout.on_loop(lambda: _spawn(asyncio.get_running_loop()))


def _move_seat(old_sub: str, new_sub: str, session_id: str = "") -> None:
    """Move one ``active_sessions`` seat between subscriptions (and swap the
    session's persisted binding row when ``session_id`` is given). The rebind's
    ``on_written`` runs on the EVENT LOOP for satellite acks (like the rotation
    fan-out's) and on a pool thread for local ones; either way the store
    writes are queued on the binding writer (``_queue_write``), in order with
    the session's bind and release, and never run on the caller's thread."""
    def _apply() -> None:
        subscription_store.increment_active_sessions(new_sub)
        subscription_store.decrement_active_sessions(old_sub)
        if session_id:
            try:
                subscription_store.update_session_binding_sub(session_id, new_sub)
            except psycopg.Error as e:
                # persisted mirror only; memory is authoritative live
                logger.debug("session binding rebind: %s", e)
    # The writer keeps it in order with the session's bind and release.
    _queue_write("moving a seat", _apply)


def _move_scope_group(
    old_sub: str,
    layer: str,
    scope_sub: str,
    sids: list[str],
    *,
    cause: Cause,
    reason: str,
    replacements: dict[tuple, SubscriptionHandle | None],
    moved: list[str],
    stuck_log,
    facts: _ScopeFacts | None = None,
) -> bool:
    """Move one group of live sessions (the sessions that share one sign-in
    file) off ``old_sub`` for ``cause``, by the means its engine takes
    (``pool_moves``): a rewrite of the group's file over the fan-out for an
    engine that takes another account's sign-in in place, else a close of
    each chat's session once its turn has ended. The shared machinery behind
    the delisting rebind and the scope rebalance. ``replacements`` caches one
    replacement per (old account, context, provider, models, cause) for the
    caller's pass. The replacement must be one ``pool_moves.accepts`` for the
    cause: under every mark for a mark or a return, not refused after a
    refusal or a delisting, never the old account. Returns whether a
    replacement was found. Fail-soft everywhere; binding and seat swaps are
    ack-gated per session."""
    from services.engines import token_fanout

    facts = facts if facts is not None else _scope_facts(sids)
    in_place = _takes_sign_in_in_place(layer)
    if cause is Cause.RETURN and not in_place:
        return False  # an engine that restarts goes back at its next start, never by a close
    if not in_place and len(sids) > 1 and cause is Cause.NEAR:
        # One sign-in file under several live sessions of an engine that
        # restarts: closing them one by one would put each back on the pin.
        # They leave together at a refusal or a delisting.
        stuck_log(f"Pool: subscription {old_sub[:8]} {cause.value}: {len(sids)} live sessions on "
                  f"{layer} share one sign-in file and stay until a refusal")
        return False
    swappable = [s for s in sids if token_fanout.session_target(s)]
    if in_place and not swappable:
        stuck_log(
            f"Pool: subscription {old_sub[:8]} {cause.value} but its {len(sids)} bound "
            f"session(s) carry env-injected credentials — they follow the new "
            f"selection at their next spawn"
        )
        return False
    old_row = subscription_store.get_subscription(old_sub) or {}
    provider = str(old_row.get("provider") or "")
    models = facts.models
    model = next((m for m in sorted(models) if m), "")
    rkey = (old_sub, layer, scope_sub, provider, models, cause)
    if rkey not in replacements:
        candidates = _eligible_candidates(layer, scope_sub, provider)
        exclude: frozenset[str] = frozenset()
        if cause is not Cause.RETURN:
            exclude = pool_moves.excluded(cause, old_sub, candidates, layer, models)
        old = {"id": old_sub, "layer": layer, "auth_type": str(old_row.get("auth_type") or "oauth")}
        handle = None
        # The acquisition picks for one model of the group and ``accepts``
        # judges them all: a pick that fails one model is left out and the
        # next asked, once per candidate.
        for _ in range(len(candidates)):
            # Live sessions are re-homed, never capped: a cap gates new spawns only.
            handle = acquire_subscription(layer, scope_sub or None, provider=provider,
                                          model=model, enforce_caps=False, exclude=exclude)
            if handle is None:
                break
            # Cancel acquire's built-in +1 either way: counters move per
            # session below, only for writes that actually land.
            subscription_store.decrement_active_sessions(handle.subscription_id)
            if handle.subscription_id == old_sub:
                # The pool's own pick is the old account: a return with nothing better.
                handle = None
                break
            row = {"id": handle.subscription_id, "layer": handle.layer, "auth_type": handle.auth_type}
            if pool_moves.accepts(cause, row, models, old=old):
                break
            # Past a mark for one of the group's models, unread, or refused:
            # never move between two accounts past their marks.
            exclude = exclude | {handle.subscription_id}
            handle = None
        replacements[rkey] = handle
    handle = replacements[rkey]
    if handle is None:
        stuck_log(
            f"Pool: subscription {old_sub[:8]} {cause.value} with no account to move to "
            f"for {len(sids)} bound session(s) (layer={layer}, "
            f"scope={'user' if scope_sub else 'agent'}"
            + (f", models={sorted(m for m in models if m)}" if any(models) else "") + ") — they stay"
        )
        return False

    words = pool_moves.window_words(old_sub, layer, models, cause)
    old_name, _ = pool_moves.names(old_sub, "", scope_sub)
    if not in_place:
        chats = sorted({facts.chats[s] for s in sids if s in facts.chats})
        if chats:
            pool_moves.restart_when_idle(chats, pool_moves.line(cause, old_name, "", words, in_place=False))
        logger.info("Pool: %s session(s) on %s leave subscription %s (%s) when idle: their next start "
                    "takes the pool's pick", len(chats), layer, old_sub[:8], cause.value)
        return True

    # The replacement's credential FILE, from the token this handle actually
    # issued (the pair the snapshot below records): None for a key or an
    # endpoint (frozen at the start, undeliverable into a live file session).
    payload = None
    if handle.oauth_access_token:
        from core.session.session_manager import get_layer_by_path
        payload = get_layer_by_path(layer).credential_file_payload(
            handle.oauth_access_token, handle.oauth_expires_at_ms, handle.credential,
        )
    deliverable = list(swappable) if payload is not None else []
    if not deliverable:
        stuck_log(
            f"Pool: replacement {handle.subscription_id[:8]} for {cause.value} "
            f"{old_sub[:8]} has no file-deliverable credential for "
            f"{len(swappable)} live session(s) — they keep their current "
            f"credentials until their next spawn"
        )
        return True

    new_sub = handle.subscription_id
    new_expiry = handle.oauth_expires_at_ms
    old_name, new_name = pool_moves.names(old_sub, new_sub, scope_sub)
    message = pool_moves.line(cause, old_name, new_name, words, in_place=True)

    def _on_written(
        sid: str, *, _old=old_sub, _new=new_sub, _exp=new_expiry,
    ) -> None:
        # Fires per session once its file write landed (sync for local
        # dirs, satellite-ack for remote) — swap the binding only then,
        # and only if the session wasn't released or re-homed meanwhile.
        with _session_maps_lock:
            if _session_subscriptions.get(sid) != _old:
                return
            _session_subscriptions[sid] = _new
            _session_moved_at[sid] = time.monotonic()
            _session_bound_at[sid] = time.time()
            if _exp:
                _session_token_expiry[sid] = _exp
            else:
                _session_token_expiry.pop(sid, None)
        _move_seat(_old, _new, sid)
        moved.append(sid)
        if sid in facts.chats:
            pool_moves.note(facts.chats[sid], message)
        # A terminal at its limit is free of it: its hold ends, and a turn
        # the limit left waiting for an account goes on.
        from services.engines import pool_limits
        pool_limits.account_freed(sid, _old)
        logger.info(
            "Pool: re-homed session %s onto subscription %s (was %s, %s%s)",
            sid[:8], _new[:8], _old[:8], cause.value, f"; {reason}" if reason else "",
        )

    token_fanout.fan_out(
        deliverable, layer=layer, payload=cast("dict", payload),  # deliverable is empty without one
        on_written=_on_written, expected_sub_id=old_sub,
    )
    return True


def _eligible_candidates(layer: str, scope_sub: str, provider: str = "") -> list[dict]:
    """The candidate rows an acquisition with this context would consider —
    the exact lists ``acquire_subscription`` reads (user scope: own accounts,
    then the borrowable platform pool behind the Platform-Auth toggle; agent
    scope: the full platform pool). Used by the drift trigger to ask "is there
    somewhere meaningfully colder to move to?" before committing a move."""
    if scope_sub:
        cands = list(subscription_store.list_personal(layer, scope_sub, provider or None))
        if subscription_store.get_user_allow_platform_auth(scope_sub):
            cands += [
                c for c in subscription_store.list_platform_pool(layer, provider or None)
                if c.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES
            ]
        return cands
    return subscription_store.list_platform_pool(layer, provider or None)


def rebalance_scopes(*, reason: str = "") -> int:
    """Move live sessions off an account that should no longer carry them —
    the counterpart to ``rebind_delisted_sessions`` for accounts that are
    still SELECTED. A group (the sessions that share one sign-in file: one
    session when it has a folder of its own) moves for one cause
    (``pool_moves.Cause``):

      - LIMIT: the vendor refused the account (a rest after a real limit,
        account-wide or for one of the group's models). Always eligible.
      - NEAR: a window the group's models count against is at its mark
        (``SPILL_PCT``). Only onto an account under every mark.
      - RETURN: the group's chats have been idle past the engine's cache
        lifetime with no turn open, every session has a chat, and an account
        whose quota resets sooner can take them. Only for an engine that
        moves in place.

    Spending alone never moves a live session. The per-scope cooldown holds
    RETURN and is stamped only when a return found a replacement.
    Sessions without a scope key (Direct LLM: env-frozen credentials) or
    without a stamped acquisition context never move. Serialized with the
    delisting rebind on ``_rebind_lock``. Returns sessions whose swap landed
    synchronously (satellite acks land later). Never raises. Sync — call via
    ``asyncio.to_thread``.
    """
    try:
        return _rebalance_scopes(reason=reason)
    except Exception:
        logger.exception("Pool: scope rebalance pass failed")
        return 0


def _prune_scope_maps(live_keys: set[str]) -> None:
    """A scope key is a session's own folder now: drop the cooldown stamps of
    keys no live session holds, and the claims past their window."""
    for key in [k for k in _scope_rebalance_last if k not in live_keys]:
        _scope_rebalance_last.pop(key, None)
    cutoff = time.time() - _SCOPE_RECENT_TTL_S
    with _session_maps_lock:
        for key in [k for k, (_s, at) in _scope_recent.items() if at < cutoff]:
            _scope_recent.pop(key, None)


def _cause_for(old_sub: str, layer: str, facts: _ScopeFacts) -> Cause | None:
    row = {"id": old_sub, "layer": layer, "auth_type": "oauth"}
    if pool_moves.refused(old_sub, layer, facts.models):
        return Cause.LIMIT
    if pool_moves.past_mark(row, facts.models):
        return Cause.NEAR
    if not facts.turn_open and pool_moves.cold(facts.last_active, _cache_lifetime_s(layer)):
        return Cause.RETURN
    return None


def _rebalance_scopes(*, reason: str) -> int:
    with _rebind_lock:
        with _session_maps_lock:
            bindings = dict(_session_subscriptions)
            ctxs = dict(_session_binding_ctx)
            scope_keys = dict(_session_scope_keys)

        # Group by (account, acquisition context, credential scope): a move
        # must carry exactly the sessions sharing that scope's sign-in file.
        groups: dict[tuple[str, str, str, str], list[str]] = {}
        for sid, old_sub in bindings.items():
            ctx = ctxs.get(sid)
            scope_key = scope_keys.get(sid) or ""
            if ctx is None or not scope_key:
                continue  # unstamped or file-less — excluded (fail-soft)
            layer, scope_sub = ctx
            groups.setdefault((old_sub, layer, scope_sub, scope_key), []).append(sid)
        _prune_scope_maps({key for (_o, _l, _s, key) in groups})

        moved: list[str] = []
        replacements: dict[tuple, SubscriptionHandle | None] = {}
        stuck_log = logger.info if reason else logger.debug
        now_mono = time.monotonic()
        for (old_sub, layer, scope_sub, scope_key), sids in groups.items():
            with _session_maps_lock:
                recent = _scope_recent.get(scope_key)
            if recent and recent[0] == old_sub and time.time() - recent[1] <= _SCOPE_RECENT_TTL_S:
                # A spawn just claimed this scope on THIS account
                # (acquire→bind window): moving the scope NOW would race the
                # spawn's own credential-file write. The next tick retries
                # once the spawn has bound. A claim on ANOTHER account is a
                # sticky pin that yielded (``_select_sticky``): the rest of the
                # scope must follow it, and its file already carries that account.
                continue
            facts = _scope_facts(sids)
            cause = _cause_for(old_sub, layer, facts)
            if cause is None:
                continue
            last = _scope_rebalance_last.get(scope_key)
            if cause.cooled and last is not None and now_mono - last < _SCOPE_REBALANCE_COOLDOWN_S:
                continue
            if _move_scope_group(
                old_sub, layer, scope_sub, sids,
                cause=cause, reason=reason, facts=facts,
                replacements=replacements, moved=moved, stuck_log=stuck_log,
            ) and cause.cooled:
                _scope_rebalance_last[scope_key] = now_mono
        return len(moved)
