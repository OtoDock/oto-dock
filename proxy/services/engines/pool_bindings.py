"""A session's binding to its account: the scope key, the scope's committed account, bind, release,
seats, attribution and the post-restart restore.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection
from concurrent.futures import Future

import psycopg

from core import placement
from services.engines.pool_state import (
    _ACTIVITY_AUTH_TYPES,
    _SCOPE_RECENT_TTL_S,
    _issued_token_expiry,
    _scope_recent,
    _session_binding_ctx,
    _session_bound_at,
    _session_maps_lock,
    _session_models,
    _session_moved_at,
    _session_scope_keys,
    _session_subscriptions,
    _session_token_expiry,
    _start_token_expiry,
)
from services.engines.pool_writer import (
    _guarded,
    _queue_write,
    flush_binding_writes,
)
from storage.billing import subscription_store

logger = logging.getLogger(__name__)


# Module import ≈ proxy boot. For the first minutes after a restart the live
# session registries are still warming (satellites reconnect and re-announce
# their surviving sessions lazily) — a persisted binding row must not be
# judged dead, let alone deleted, before its session had a chance to reappear.
_BOOT_MONOTONIC = time.monotonic()


_LIVENESS_BOOT_GRACE_S = 600.0


def within_boot_grace() -> bool:
    """True while the live-session registries are still warming after a proxy
    restart. Workers that act on a row *looking* unbound (e.g. the proactive
    unbound-row token refresh) must stand down during this window — a
    surviving satellite session's row is indistinguishable from a truly idle
    one until it re-announces."""
    return time.monotonic() - _BOOT_MONOTONIC < _LIVENESS_BOOT_GRACE_S


def session_registered_live(session_id: str) -> bool | None:
    """Is this session live in ANY registry that can hold a bound session?
    Checks the pool's own live map, the interactive registry (local + remote
    PTYs, incl. re-adopted ones), then every execution layer through the
    registry (``find_layer_for_session``: the local engines and the remote
    layer, incl. post-restart adopted sessions — ``adopt_session``
    re-registers there without re-binding, which is exactly why the
    persisted rows exist; it never creates the remote layer). Returns None
    when a registry can't be consulted (import/attr failure) — the caller
    must fail SOFT and treat the session as live: wrongly deleting a live
    session's row breaks its usage attribution and scope pin."""
    try:
        if session_id in _session_subscriptions:
            return True
        from core.session.session_state import session_is_held
        return session_is_held(session_id)
    except ImportError:
        return None


def session_gone(session_id: str) -> bool:
    """Whether a session is gone for good, for what is removed with it (its
    home and the sign-in in it): no registry holds it
    (``session_registered_live``), and nothing may still bring it back.
    A session with a stored binding may come back while the machine its key
    names is away or has not reported the sessions it kept since this start
    (``_may_come_back``). One with no stored binding is judged by the
    registries alone, so a live terminal whose row a restart pruned stays.
    A registry or a row that cannot be read answers no: the caller keeps
    what it holds."""
    if session_registered_live(session_id) is not False:
        return False
    try:
        row = subscription_store.get_session_binding(session_id)
    except psycopg.Error:
        return False
    if not isinstance(row, dict):
        return True
    return not _may_come_back(str(row.get("scope_key") or ""))


def _may_come_back(scope_key: str) -> bool:
    """Whether a session bound under ``scope_key`` may still be on its way
    back to the registries: the machine its key names (any compute machine
    for the platform's place) is away, or has not reported the sessions it
    kept since this start (``satellite_link.reported_since_boot``). A
    machine list that cannot be read answers yes."""
    from core.remote import satellite_link
    from core.remote.satellite_connection import get_connection_manager
    machine = placement.machine_of(scope_key.split(":", 1)[0]) if ":" in scope_key else ""
    if machine:
        machines = [machine]
    else:
        from storage import remote_store
        try:
            machines = [str(m.get("id") or "") for m in remote_store.list_compute_machines()]
        except psycopg.Error:
            return True
    cm = get_connection_manager()
    return any(not cm.is_connected(m) or not satellite_link.reported_since_boot(m) for m in machines)


def drop_ghost_binding(session_id: str) -> None:
    """Delete the stored binding of a session that is gone: its release
    never ran (both units restarted together, a session the platform
    declined after a restart)."""
    _queue_write("dropping a ghost binding", subscription_store.delete_session_binding, session_id)


def credential_scope_key(target: str, host_dir: str) -> str:
    """Identity of a credential-file-sharing domain. Sessions with the same key
    read/write the SAME ``.credentials.json`` / ``auth.json`` (the host config
    dir is per (agent, scope user, CLI flavor) — see
    ``ensure_persistent_agent_dir``), so they must all run on the same account:
    two accounts alternating over one file is the last-write-wins /
    silent-401-flip hazard. ``target`` keeps a satellite's mirrored dir from
    colliding with the local one in the key space; '' for no domain (e.g.
    direct-llm — env-injected keys, nothing shared on disk)."""
    if not host_dir:
        return ""
    return f"{target or placement.LOCAL}:{host_dir}"


def scope_key_for(target: str, store: str, home: str = "") -> str:
    """The key of the sign-in file a session reads: its own home's when it
    runs from one (``core.sandbox.session_home``: unique to the session, so
    it shares an account with no other), else its scope folder's, which
    every session of the scope shares. Every builder passes it at acquire
    and the layer binds with the same call, so the two always agree."""
    return credential_scope_key(target, home or store)


def bound_folder(session_id: str) -> str:
    """The sign-in folder a bound session reads, as its scope key names it
    (``scope_key_for``: its own home, or its scope's folder); "" unbound."""
    key = _session_scope_keys.get(session_id, "")
    return key.split(":", 1)[1] if ":" in key else ""


def folder_bound(folder: str) -> bool:
    """Whether a session may still read ``folder`` itself as its sign-in
    folder, so a home start keeps the store's own files
    (``codex.spawn._make_home``). A session bound to the folder
    (``bound_folder``: one that runs from its scope's folder, not a home)
    counts on a machine that works on the platform's own folders, and on
    one not connected now, whose session may come back. A paired computer
    or a compute machine with its own copy reads its own disk, which the
    removal never reaches. After a restart the map is whole only once each
    compute machine beside the platform has reported the sessions it kept
    (``_reports_owed``): until then every folder counts."""
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    with _session_maps_lock:
        keys = [key.split(":", 1) for key in _session_scope_keys.values() if ":" in key]
    for target, bound in keys:
        machine = placement.machine_of(target)
        if bound == folder and (not machine or not cm.is_connected(machine) or cm.shares_folders(machine)):
            return True
    return _reports_owed()


def _reports_owed() -> bool:
    """Within a restart's grace, whether a compute machine beside the
    platform has not yet reported the sessions it kept
    (``satellite_link.reported_since_boot``). An unreadable list counts as
    owed. A machine that never comes back holds nothing past the grace."""
    if not within_boot_grace():
        return False
    from core.remote import satellite_link
    from storage import remote_store
    try:
        machines = remote_store.list_compute_machines()
    except psycopg.Error:
        return True
    return any(not satellite_link.reported_since_boot(str(m.get("id") or ""))
               for m in machines if not placement.compute_own_copy(m))


def _sticky_subscription_id(scope_key: str) -> str | None:
    """The subscription the credential scope is already committed to, or None.

    Three sources, strongest first: a LIVE bound session's scope key (the
    in-memory maps), a just-acquired-not-yet-bound spawn (``_scope_recent`` —
    the acquire→bind race window), then the persisted bindings (post-restart:
    the maps are empty but a surviving session — remote satellite, re-adopted
    chat — may still hold the scope's file).

    Persisted rows are trusted only for LIVE sessions: a row whose session is
    in no registry is a ghost from an un-clean kill (proxy restart mid-turn,
    satellite death) and gets deleted on sight — before this check a single
    ghost pinned its scope to one account for the full startup-prune TTL (up
    to 7 days), which is how a busy agent's scope never re-consulted headroom
    (live-observed 2026-07-11). Within the post-boot grace window the verdict
    is skipped (registries still warming; behave like the pre-check code)."""
    with _session_maps_lock:
        # Newest first, as the persisted rows below: after a refusal one
        # session of a shared folder restarted on a new account while an older
        # neighbour waits on the refused one, and the folder holds the new one.
        live = sorted((sid for sid, sk in _session_scope_keys.items() if sk == scope_key),
                      key=lambda s: _session_bound_at.get(s, 0.0), reverse=True)
        for sid in live:
            sub = _session_subscriptions.get(sid)
            if sub:
                return sub
        recent = _scope_recent.get(scope_key)
        if recent:
            sub, ts = recent
            if time.time() - ts <= _SCOPE_RECENT_TTL_S:
                return sub
            _scope_recent.pop(scope_key, None)
    from core.sandbox import session_home
    if ":" in scope_key and session_home.is_home(scope_key.split(":", 1)[1]):
        # A home has one session: a stored row of its key is the restarting
        # session's own, whose account a move was leaving.
        return None
    if time.monotonic() - _BOOT_MONOTONIC < _LIVENESS_BOOT_GRACE_S:
        try:
            sub = subscription_store.get_scope_binding(scope_key)
        except psycopg.Error:
            return None
        return sub if isinstance(sub, str) and sub else None
    try:
        rows = list(subscription_store.list_scope_bindings(scope_key) or [])
    except psycopg.Error:
        return None
    for row in rows:  # newest first
        sid = row.get("session_id") or ""
        if sid and session_registered_live(sid) is False:
            drop_ghost_binding(sid)
            logger.info(
                "Pool: dropped ghost binding of dead session %s "
                "(scope no longer pinned by it)", sid[:8]
            )
            continue
        sub = row.get("subscription_id")
        if isinstance(sub, str) and sub:
            return sub
    return None


def _release_rows(session_id: str, sub_id: str | None) -> None:
    # Each statement on its own: a failed delete must not keep the seat.
    _guarded("deleting a session binding", subscription_store.delete_session_binding, session_id)
    if sub_id:
        subscription_store.decrement_active_sessions(sub_id)


def release_subscription(session_id: str) -> None:
    """Release the subscription held by a session."""
    from services.engines import token_fanout
    token_fanout.unregister_session_target(session_id)
    with _session_maps_lock:
        _session_token_expiry.pop(session_id, None)
        _session_binding_ctx.pop(session_id, None)
        _session_scope_keys.pop(session_id, None)
        _session_models.pop(session_id, None)
        _session_moved_at.pop(session_id, None)
        _session_bound_at.pop(session_id, None)
        sub_id = _session_subscriptions.pop(session_id, None)
    # The persisted mirror and the seat: queued behind the bind's own write
    # (the startup TTL prune is the backstop for a write that fails).
    _queue_write("releasing a session binding", _release_rows, session_id, sub_id)
    if sub_id:
        logger.info("Pool: released subscription %s for session %s", sub_id[:8], session_id[:8])
        # A wake its chat held back while this session served another person
        # may run now: the delivery's business, handed the id alone.
        from services.scheduler import delivery
        delivery.replay_after_release(session_id)


def release_unbound_seat(subscription_id: str, sticky_scope: str = "") -> None:
    """Give back a seat ``resolve_subscription_env`` took that no session will
    ever bind — a spawn abandoned after its config was built (a skipped
    pre-warm, a slot denial, an offline target, a failed ``start_session``)
    or a task round that rode an already-warm session. ``release_subscription``
    cannot do it: it finds no binding and decrements nothing, which is how
    ``active_sessions`` drifted up one seat per such path (public issue #3's
    "stale counter"). The acquire-window claim the spawn left on its scope
    is dropped with the seat — nothing will bind from it, so it must not
    steer a concurrent same-scope spawn. Never touches bindings: the
    callers know whether a binding under their session id is a live
    session's (keep it) or a dead one's (``release_subscription``)."""
    if not subscription_id:
        return
    _start_token_expiry.pop((subscription_id, sticky_scope), None)
    with _session_maps_lock:
        claim = _scope_recent.get(sticky_scope) if sticky_scope else None
        if claim and claim[0] == subscription_id:
            _scope_recent.pop(sticky_scope, None)
    _queue_write("releasing an unbound seat", subscription_store.decrement_active_sessions,
                 subscription_id)
    logger.info(
        "Pool: released unbound seat on subscription %s "
        "(spawn abandoned before bind)", subscription_id[:8]
    )


def session_bound(session_id: str) -> bool:
    """Whether the live map holds a binding for ``session_id``."""
    with _session_maps_lock:
        return session_id in _session_subscriptions


def live_session_count(sub_id: str) -> int:
    """Seats ``sub_id`` really holds right now: sessions bound to it in the
    live map plus fresh acquire-window claims (a spawn between its acquire
    and its bind). The honest counterpart of the store's ``active_sessions``
    counter, which only moves by increments and decrements and reads stale
    after any unbalanced path. Within the post-boot grace window the
    persisted bindings count too: a satellite session that has not
    re-announced yet is invisible to the maps but alive. Known slack: the
    claims exist only for CLI sticky spawns and one claim covers a scope,
    so a direct-llm, phone or meeting spawn is invisible between its
    acquire and its bind (seconds) — a delete in that window under-counts
    by one and the seat's release later floors at zero."""
    now = time.time()
    with _session_maps_lock:
        live_ids = {sid for sid, s in _session_subscriptions.items() if s == sub_id}
        claims = sum(
            1 for sub, ts in _scope_recent.values()
            if sub == sub_id and now - ts <= _SCOPE_RECENT_TTL_S
        )
    if within_boot_grace():
        try:
            live_ids |= set(subscription_store.list_binding_session_ids(sub_id))
        except psycopg.Error:
            logger.debug("live_session_count: persisted bindings unreadable", exc_info=True)
    return len(live_ids) + claims


def sessions_on(rows: list[dict], *, viewer: str, admin: bool,
                working: Collection[str] = ()) -> dict[str, tuple[int, list[str]]]:
    """The sessions bound to each account of a list, and the model of each
    of them that works now (``working``, read on the loop by
    ``lane_status.working_sessions``; its chat row's model, else the one it
    was bound with, "" for none): ``{id: (bound, models)}``, an account with
    none absent. The bound count orders the cards; the models are what they
    show as running. What a list may show (the AI Engines cards):

    - a person's own list (``admin`` False): the sessions acquired for
      ``viewer``, and one whose acquisition is unknown on an account not
      shared with agents; never the agents' work or a borrower's, also on an
      account that stopped being shared while they still run on it;
    - the admin's list: every session, except, on an account another admin
      owns, the sessions acquired for that admin (their own chats).

    The live map only: a session not re-announced after a restart is not
    counted. Synchronous (one query for the models)."""
    from storage import database as task_store
    with _session_maps_lock:
        bound = [(sid, sub, _session_binding_ctx.get(sid), _session_models.get(sid, ""))
                 for sid, sub in _session_subscriptions.items()]
    by_id = {r["id"]: r for r in rows if r.get("id")}
    kept: dict[str, list[tuple[str, str]]] = {}
    for sid, sub, ctx, model in bound:
        row = by_id.get(sub)
        if row is None:
            continue
        acquired_for = ctx[1] if ctx is not None else None
        owner = str(row.get("owner_sub") or "")
        if admin:
            if owner and owner != viewer and acquired_for == owner:
                continue
        elif acquired_for != viewer and (acquired_for is not None or row.get("contribute_platform")):
            continue
        kept.setdefault(sub, []).append((sid, model))
    if not kept:
        return {}
    busy = [sid for pairs in kept.values() for sid, _ in pairs if sid in working]
    try:
        chat_models = task_store.models_of_sessions(busy) if busy else {}
    except psycopg.Error:
        chat_models = {}
    return {sub: (len(pairs), [chat_models.get(sid) or model for sid, model in pairs if sid in working])
            for sub, pairs in kept.items()}


def reconcile_active_sessions(sub_id: str) -> tuple[int, int]:
    """Lower the store's ``active_sessions`` for ``sub_id`` to
    :func:`live_session_count` when it reads HIGHER; never raise it (a seat
    the live map does not see — a satellite session not yet re-announced
    after a restart — is re-taken by ``restore_session_binding`` itself).
    Returns ``(stored, live)``. Used where a stale counter would otherwise
    refuse an admin action (the subscription delete)."""
    flush_binding_writes()   # the queued releases count before the stored value is read
    row = subscription_store.get_subscription(sub_id) or {}
    stored = int(row.get("active_sessions") or 0)
    live = live_session_count(sub_id)
    if stored > live and subscription_store.lower_active_sessions(sub_id, live):
        logger.info(
            "Pool: reconciled active_sessions of subscription %s "
            "%s → %s (no live session backed the difference)", sub_id[:8], stored, live
        )
    return stored, live


def bind_session(
    session_id: str,
    subscription_id: str,
    *,
    layer: str = "",
    user_sub: str | None = None,
    scope_key: str = "",
    model: str = "",
    bound_at: float | None = None,
) -> Future:
    """Track which subscription a session is using (for cleanup + fan-out).

    ``bound_at``: when it was bound (epoch seconds), now when None. A binding
    restored after a restart keeps its own (``restore_session_binding``), so
    the order of a folder's sessions is their binds', in memory and in the
    rows, never the order they were restored in.

    ``model`` is the model the session starts with: the pool judges a session
    by its chat row's model, and by this one when it has no chat row.

    ``layer`` + ``user_sub`` record the ACQUISITION context — the arguments
    this binding's ``acquire_subscription`` call resolved with (``""`` =
    agent-scope) — so a later selection change can re-evaluate the session
    against the same candidate lists (``rebind_delisted_sessions``). The
    layers pass ``user_sub`` from ``AgentConfig.subscription_user_sub``;
    ``None`` means the spawn path didn't stamp it and the session is excluded
    from selection-change rebinds (fail-soft — never guess the scope: a
    user-scope session misjudged as agent-scope could be re-homed onto an
    admin OAuth subscription, which user scope must never borrow).

    Also snapshots the expiry of the OAuth access token this spawn wrote into
    the session's credential file: ``_issued_token_expiry`` was stamped when
    THIS spawn's credentials were resolved, and bind follows resolve within the
    same spawn flow. The snapshot deliberately captures the token actually
    issued — on a fail-soft refresh that is an aging stored token, not a
    full-runway one (exactly how the 2026-07-06 mid-turn 401s hid from every
    full-runway assumption). Rotation fan-out advances the snapshot per session
    once the session's file holds the new token.
    """
    exp = _start_token_expiry.pop((subscription_id, scope_key), None) if scope_key else None
    if exp is None:
        exp = _issued_token_expiry.get(subscription_id)
    with _session_maps_lock:
        prior_sub = _session_subscriptions.get(session_id)
        _session_subscriptions[session_id] = subscription_id
        _session_bound_at[session_id] = bound_at if bound_at is not None else time.time()
        if layer and user_sub is not None:
            _session_binding_ctx[session_id] = (layer, user_sub)
        else:
            _session_binding_ctx.pop(session_id, None)
        if scope_key:
            _session_scope_keys[session_id] = scope_key
            # The live binding supersedes the acquire-window claim.
            _scope_recent.pop(scope_key, None)
        else:
            _session_scope_keys.pop(session_id, None)
        if exp:
            _session_token_expiry[session_id] = exp
        else:
            _session_token_expiry.pop(session_id, None)
        if model:
            _session_models[session_id] = model
        else:
            _session_models.pop(session_id, None)
    # Persisted mirror: survives restarts so usage attribution
    # (get_session_subscription read-through) and the scope-sticky lookup keep
    # working for sessions that outlive the proxy process. Queued on the
    # binding writer, which keeps it ahead of this session's release (the
    # orphan a deferred write once left behind cannot happen); the spawn
    # flows await the returned future so the row is durable before the
    # session runs. Best-effort: the in-memory binding stands either way.
    # Re-bind of a live session (start_session reusing a warm process): the
    # spawn flow acquired a FRESH seat for this round, so the replaced
    # binding's seat must be released — same-sub included (two increments,
    # one binding, one eventual release = +1 leak per warm continue round).
    # Historically masked because a close always released the old binding
    # before the next round's bind; a gracefully-aborted lane's session now
    # stays warm across rounds. Only the layer spawn flows re-bind live
    # sessions: restore_session_binding no-ops on a live binding and the
    # selection rebind moves seats itself without bind_session.
    def _persist() -> None:
        # Each statement on its own: a failed upsert must not keep the
        # replaced seat.
        _guarded("persisting a session binding", subscription_store.upsert_session_binding,
                 session_id, subscription_id,
                 layer=layer, user_sub=user_sub, scope_key=scope_key, model=model,
                 **({"bound_at": _iso(bound_at)} if bound_at is not None else {}))
        if prior_sub:
            subscription_store.decrement_active_sessions(prior_sub)

    if prior_sub:
        logger.info(
            "Pool: re-bound session %s "
            "%s → %s (replaced seat released)",
            session_id[:8], prior_sub[:8], subscription_id[:8]
        )
    return _queue_write("persisting a session binding", _persist)


def bound_attribution(session_id: str) -> tuple[str | None, str | None]:
    """The subscription and the payer the IN-MEMORY maps hold for a session,
    ``None`` for each they do not know (a session outside the pool, or one
    bound before a restart and not re-adopted); no store read. The
    read-through twins are :func:`get_session_subscription` and
    :func:`get_session_payer_sub`."""
    with _session_maps_lock:
        sub = _session_subscriptions.get(session_id) or None
        ctx = _session_binding_ctx.get(session_id)
    return sub, ((ctx[1] or "") if ctx is not None else None)


def get_session_subscription(session_id: str) -> str | None:
    """Get the subscription ID bound to a session.

    Read-through: the in-memory map first (every live spawn binds there), then
    the persisted bindings — a session that outlived a proxy restart (remote
    satellite, re-adopted chat) still attributes its usage to the right
    account instead of leaking to ``source_key='default'``. The store hit is
    NOT cached back: repopulating the map would make ``release_subscription``
    decrement a seat the post-restart counter reset never counted."""
    sub = _session_subscriptions.get(session_id)
    if sub:
        return sub
    try:
        row = subscription_store.get_session_binding(session_id)
    except psycopg.Error:
        return None
    if isinstance(row, dict):
        return row.get("subscription_id") or None
    return None


def session_cost_billed(session_id: str) -> bool:
    """Whether the credential serving this session costs real money — an API
    key or the hosted relay — as opposed to a subscription's or a local
    model's activity estimate (``_ACTIVITY_AUTH_TYPES``). Drives the
    ``cost_billed`` flag on the per-turn metadata event, i.e. whether the chat
    SHOWS the cost; recording is unaffected. Same read-through as
    ``get_session_subscription``. A session with no pool binding (pool-external
    credentials) or an unreadable subscription row answers True: the kind is
    unknowable there and showing is today's behaviour."""
    sub_id = get_session_subscription(session_id)
    if not sub_id:
        return True
    try:
        row = subscription_store.get_subscription(sub_id)
    except psycopg.Error:
        logger.debug("cost_billed: subscription lookup failed", exc_info=True)
        return True
    if not isinstance(row, dict):
        return True
    return row.get("auth_type") not in _ACTIVITY_AUTH_TYPES


def restore_session_binding(session_id: str) -> str | None:
    """Re-establish the IN-MEMORY binding for a session that survived a proxy
    restart (Mode C re-adopt). The persisted row keeps attribution working
    read-through, but rotation fan-out and the freshness worker enumerate the
    in-memory maps only — without this, a re-adopted session misses every
    future token rotation. Also re-takes the seat the boot-time
    ``reset_active_sessions()`` zeroed, so a later release's decrement stays
    balanced (decrement floors at 0 either way).

    Idempotent: an already-live binding is returned untouched. Returns the
    subscription id, or None when no persisted binding exists (api-key
    sessions, pool-external credentials)."""
    with _session_maps_lock:
        live = _session_subscriptions.get(session_id)
    if live:
        return live
    try:
        row = subscription_store.get_session_binding(session_id)
    except Exception:
        logger.exception("Pool: reading persisted binding failed on restore")
        return None
    if not isinstance(row, dict) or not row.get("subscription_id"):
        return None
    sub_id = row["subscription_id"]
    bind_session(
        session_id, sub_id,
        layer=row.get("layer") or "",
        user_sub=row.get("user_sub"),
        scope_key=row.get("scope_key") or "",
        model=row.get("model") or "",
        bound_at=_epoch(str(row.get("bound_at") or "")),
    )
    _queue_write("re-taking a seat on restore", subscription_store.increment_active_sessions, sub_id)
    logger.info(
        "Pool: restored binding for re-adopted session %s -> %s",
        session_id[:8], sub_id[:8],
    )
    return sub_id


def get_session_payer_sub(session_id: str) -> str:
    """The ``user_sub`` whose subscription this session acquired.

    ``""`` means platform/agent-pool paid (agent-scope acquisition) or
    unknown (no binding — e.g. credentials outside the pool). Same
    read-through as :func:`get_session_subscription`: the in-memory
    acquisition context first, then the persisted binding so usage
    attribution survives a proxy restart. Usage recording uses this to
    bill a Shared-only agent's human chats to the interacting user
    (whose subscription actually served the turn) instead of the agent
    bucket.
    """
    with _session_maps_lock:
        ctx = _session_binding_ctx.get(session_id)
    if ctx is not None:
        return ctx[1] or ""
    try:
        row = subscription_store.get_session_binding(session_id)
    except psycopg.Error:
        return ""
    if isinstance(row, dict):
        return row.get("user_sub") or ""
    return ""


def session_token_expiry_ms(session_id: str) -> int | None:
    """``expiresAt`` (epoch ms) of the OAuth access token in this session's
    credential file (spawn snapshot, advanced by fan-out) — None for sessions
    with no expiring credential (api_key / local_endpoint / relay) and for
    sessions spawned before the proxy last restarted (the map is in-memory;
    callers must treat None as "unknown", not "immortal")."""
    return _session_token_expiry.get(session_id)


def bound_since(session_id: str) -> float:
    """When the session was bound to its account (epoch seconds), a move
    included: the in-memory stamp, else the persisted row's ``bound_at`` (a
    session that outlived a restart), else 0."""
    at = _session_bound_at.get(session_id)
    if at is not None:
        return at
    try:
        row = subscription_store.get_session_binding(session_id)
    except psycopg.Error:
        return 0.0
    raw = str((row or {}).get("bound_at") or "") if isinstance(row, dict) else ""
    return _epoch(raw) or 0.0


def _epoch(iso: str) -> float | None:
    """A row's ``bound_at`` as epoch seconds, None when it holds none."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def moved_within(session_id: str, seconds: float) -> bool:
    """Whether a move swapped the session's binding less than ``seconds``
    ago (``pool_acquire._move_scope_group``)."""
    at = _session_moved_at.get(session_id)
    return at is not None and time.monotonic() - at < seconds


def bound_oauth_subscription_ids() -> set[str]:
    """Subscription ids with at least one live bound session AND an expiring
    token snapshot — the freshness worker's work list. Keying on the expiry
    snapshots (only stamped for expiring OAuth credentials) skips api_key /
    local / relay sessions without a store read per tick."""
    with _session_maps_lock:
        return {
            _session_subscriptions[sid]
            for sid in list(_session_token_expiry)
            if sid in _session_subscriptions
        }
