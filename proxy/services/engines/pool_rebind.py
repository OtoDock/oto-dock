"""The re-homing of live sessions whose account left the selection.
"""

from __future__ import annotations

import asyncio
import logging

from core.execution_layer import SubscriptionHandle
from services.engines.pool_acquire import (
    _move_scope_group,
    _rebind_lock,
    _rebind_tasks,
)
from services.engines.pool_moves import Cause
from services.engines.pool_state import (
    _USER_BORROWABLE_AUTH_TYPES,
    _session_binding_ctx,
    _session_maps_lock,
    _session_scope_keys,
    _session_subscriptions,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)


def schedule_rebind(reason: str) -> None:
    """Fire-and-forget a ``rebind_delisted_sessions`` pass from an async
    context — called by the API endpoints right after a selection mutation
    (scope checkboxes, subscription add/delete, Platform-Auth toggle, role
    change, OAuth connect) so live sessions follow the change within moments
    instead of waiting for the next freshness tick. Never blocks the caller
    (a replacement acquisition can involve a network token refresh); the
    worker's per-tick pass is the retry loop, so a lost task only delays
    convergence by ≤5 min."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # sync caller (tests / scripts) — the worker pass converges
    task = loop.create_task(
        asyncio.to_thread(rebind_delisted_sessions, reason=reason)
    )
    _rebind_tasks.add(task)
    task.add_done_callback(_rebind_tasks.discard)


def _selection_contains(sub_id: str, layer: str, scope_sub: str) -> bool:
    """True while ``sub_id`` is still part of the CURRENT selection for an
    acquisition context — membership in the same candidate lists
    ``acquire_subscription`` reads (``scope_sub=""`` = agent-scope). No
    provider filter: it only narrows the candidate set and a bound row always
    matches its own provider, so membership is filter-invariant. Throttling
    is ignored — a resting account is de-prioritised for NEW acquisitions,
    not delisted."""
    if scope_sub:
        if any(s["id"] == sub_id for s in subscription_store.list_personal(layer, scope_sub)):
            return True
        if not subscription_store.get_user_allow_platform_auth(scope_sub):
            return False
        return any(
            s["id"] == sub_id and s.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES
            for s in subscription_store.list_platform_pool(layer)
        )
    return any(s["id"] == sub_id for s in subscription_store.list_platform_pool(layer))


def rebind_delisted_sessions(*, reason: str = "") -> int:
    """Re-home live sessions bound to a subscription that is no longer part of
    their scope's selection — the owner unticked ``use_personal``, an admin
    unticked ``contribute_platform`` or disabled the row, the row was deleted
    or auto-expired, or the user's Platform Auth was revoked. Without this,
    bindings made at spawn outlive the selection: the freshness worker keeps
    the OLD account's token fresh forever and long-lived sessions (interactive
    terminals survive their window closing) never see the newly selected
    account until a proxy restart.

    For each affected session with a registered credential FILE (the
    hot-swappable set: Claude ``.credentials.json`` / Codex ``auth.json``,
    local or satellite) a currently-eligible replacement is acquired via the
    normal ``acquire_subscription`` path and the file is rewritten in place
    over the rotation fan-out rails — the live CLI re-reads it, no respawn.
    The binding + expiry-snapshot swap is ack-gated per session
    (``on_written``): a failed write keeps the old binding so the next pass
    retries. ``active_sessions`` counters move per landed session.

    Fail-soft everywhere: with no eligible replacement — or a replacement
    whose credential can't reach a live process (env-injected API key /
    endpoint) — the session keeps its current credentials (they aren't
    revoked by deselection) and follows the new selection at its next spawn.
    Runs after every selection mutation (``schedule_rebind``) and at the top
    of every freshness tick (the retry/convergence loop). Returns the number
    of sessions whose rebind landed synchronously (satellite acks land
    later). Never raises (background primitive — the worker retries anyway).
    Sync — call via ``asyncio.to_thread``.
    """
    try:
        return _rebind_delisted_sessions(reason=reason)
    except Exception:
        logger.exception("Pool: selection rebind pass failed")
        return 0


def _rebind_delisted_sessions(*, reason: str) -> int:
    with _rebind_lock:
        with _session_maps_lock:
            bindings = dict(_session_subscriptions)
            ctxs = dict(_session_binding_ctx)
            scope_keys = dict(_session_scope_keys)

        # Group by (old sub, acquisition context, sign-in file) so the
        # sessions that share one file move together, and each group is
        # judged by its own sessions' models. A session with no scope key is a
        # group of its own.
        groups: dict[tuple[str, str, str, str], list[str]] = {}
        for sid, old_sub in bindings.items():
            ctx = ctxs.get(sid)
            if ctx is None:
                continue  # unstamped binding — excluded (fail-soft)
            layer, scope_sub = ctx
            groups.setdefault((old_sub, layer, scope_sub, scope_keys.get(sid) or sid), []).append(sid)

        moved: list[str] = []
        replacements: dict[tuple, SubscriptionHandle | None] = {}
        # A user action (reason set) logs stuck sessions loudly once; the
        # 5-minute worker pass retries the same situation quietly.
        stuck_log = logger.info if reason else logger.debug
        for (old_sub, layer, scope_sub, _key), sids in groups.items():
            if _selection_contains(old_sub, layer, scope_sub):
                continue
            _move_scope_group(
                old_sub, layer, scope_sub, sids,
                cause=Cause.DELISTED, reason=reason,
                replacements=replacements, moved=moved, stuck_log=stuck_log,
            )
        return len(moved)
