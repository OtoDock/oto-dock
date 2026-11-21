"""What a builder asks before a spawn: the cap, borrowing, whether a user or a layer can run, and the
engine an agent's creator gets.
"""

from __future__ import annotations

from services.engines.pool_rests import (
    _is_throttled,
)
from services.engines.pool_select import (
    _cap_status,
    _rows_without_login,
)
from services.engines.pool_state import (
    _USER_BORROWABLE_AUTH_TYPES,
)
from storage.agents.schema import DEFAULT_EXECUTION_PATH
from storage.billing import subscription_status, subscription_store


def cap_continue_available(layer: str, user_sub: str | None, *, provider: str = "") -> bool:
    """Under a pool cap's ``continue``, is there a non-OAuth credential the
    spawn could take? User scope: an own key or endpoint, else a borrowable
    platform credential; agent scope: a non-OAuth row in the pool. No
    acquisition, no token mint (the friendly gates ask before recycling)."""
    if user_sub:
        own = subscription_store.list_personal(layer, user_sub, provider or None)
        if _rows_without_login(own):
            return True
        return borrowable_pool_available(layer, user_sub, provider=provider)
    return bool(_rows_without_login(subscription_store.list_platform_pool(layer, provider or None)))


def user_scope_block_reason(layer: str, user_sub: str, *, provider: str = "") -> str:
    """Classify why a user-scoped acquisition found no credentials, for the
    dashboard "no subscription" message. Cheap; called only on the terminal
    blocked path. Returns one of:
      'pool_cap'         — the user's own pool is at its subscription cap
                           (checked first: a capped user owns working accounts
                           and would otherwise read as auth_off / no_pool)
      'throttled'        — the user owns a sub for this layer but it's resting
                           (recent provider rate-limit/overload) — transient, retry
      'own_sub_expired'  — the user's own account(s) here are expired (dead
                           login grant) — reconnecting revives them in place
      'auth_off'         — Platform Auth disabled and the user has no own sub
      'admin_oauth_only' — pool exists but holds only OAuth subs (not borrowable)
      'no_pool'          — nothing in the platform pool for this layer at all
      'none'             — a borrowable sub exists but couldn't yield creds (glitch)

    Post-failure classification ONLY — never a pre-acquisition gate: a user
    whose own row expired but who can borrow an admin api_key row still gets a
    working session and never reaches this.
    """
    cap = _cap_status(layer, user_sub)
    if cap is not None and not cap.allowed:
        return "pool_cap"
    # The user DOES own a sub here, it's just resting — never tell them to "connect
    # an account". (With _select's throttled-fallback this rarely reaches a block,
    # but keep the classification honest for any caller.)
    own = subscription_store.list_personal(layer, user_sub, provider or None)
    if own and all(_is_throttled(s["id"]) for s in own):
        return "throttled"
    if not own:
        # No usable own account — but a dead one the user could revive beats
        # every "connect an account" message. Specifically `expired` rows
        # (grant death); an admin-DISABLED row must not say "reconnect".
        stale = subscription_store.list_personal(
            layer, user_sub, provider or None, any_status=True,
        )
        if any(s.get("status") == subscription_status.EXPIRED for s in stale):
            return "own_sub_expired"
    if not subscription_store.get_user_allow_platform_auth(user_sub):
        return "auth_off"
    pool = subscription_store.list_platform_pool(layer, provider or None)
    if not pool:
        return "no_pool"
    if any(s.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES for s in pool):
        return "none"
    return "admin_oauth_only"


def borrowable_pool_available(layer: str, user_sub: str, *, provider: str = "") -> bool:
    """True if the user may borrow a platform API credential for this layer —
    Platform Auth on AND a borrowable admin sub exists (api_key/relay/local; never
    an admin OAuth subscription). No acquisition / no token mint."""
    if not subscription_store.get_user_allow_platform_auth(user_sub):
        return False
    pool = subscription_store.list_platform_pool(layer, provider or None)
    return any(s.get("auth_type") in _USER_BORROWABLE_AUTH_TYPES for s in pool)


def user_can_run(layer: str, user_sub: str, *, provider: str = "") -> bool:
    """True if a user-scoped request on ``layer`` would resolve to SOME credential:
    the user has an own usable account, OR a borrowable platform sub is available.
    Mirrors ``acquire_subscription``'s user-scope branch without acquiring/minting —
    the single predicate behind the SetupBanner and the per-layer availability flag."""
    if subscription_store.list_personal(layer, user_sub, provider or None):
        return True
    return borrowable_pool_available(layer, user_sub, provider=provider)


def layer_platform_configured(layer: str) -> bool:
    """True when the platform POOL has a subscription for ``layer`` — an
    active ``contribute_platform`` row owned by a current admin, or owner-less
    platform infra like the hosted relay (:func:`subscription_store.list_platform_pool`).

    This is the agent engine-enablement gate's predicate (operator decision
    2026-07-25, revised post-review): agent-scoped background work —
    scheduled tasks, phone conversations, triggers, anything with no driving
    user — can ONLY run on the pool, so an engine enabled on the strength of
    one user's personal connection would break the moment any non-user-scoped
    run touches it. Personal connections deliberately do NOT count here; they
    serve only that user's own chats, and per-user visibility is
    :func:`user_can_run`'s job at chat time. Still deliberately weaker than
    run-time credential resolution ("is enabling this sensible", not "will
    this specific run resolve a credential")."""
    return bool(subscription_store.list_platform_pool(layer))


def _auto_enable_candidates() -> list[str]:
    """The engines a fresh agent may be auto-enabled on, in priority order:
    the CODING engines (``identity.role``), in AI Engines page order
    (``identity.sort_order``). A supporting engine — Direct LLM — is never
    the auto-pick: it needs an explicit model + provider to be useful, and
    the agent-scope pool path can't borrow an admin OAuth sub the way the
    CLIs can; a creator can still enable it by hand in the agent's Config
    tab. The page order doubling as the priority is deliberate: an engine
    that should display second but never be auto-picked would need a field
    of its own, and no such engine exists."""
    from core.session.session_manager import get_all_layers
    coding = [
        (layer.capabilities.identity.sort_order, path)
        for path, layer in get_all_layers().items()
        if layer.capabilities.identity.role == "coding"
    ]
    return [path for _order, path in sorted(coding)]


def default_execution_layer_for_creator(user_sub: str) -> str:
    """Pick the execution layer (AI engine) to auto-enable for an agent that
    ``user_sub`` is creating or installing, so the agent runs zero-config.

    Returns the first coding engine (:func:`_auto_enable_candidates` — Claude
    Code, then Codex) that is connected on BOTH sides:

      - the PLATFORM — an admin has contributed an active subscription for it to
        the shared pool (:func:`subscription_store.list_platform_pool`), so
        agent-scope sessions (scheduled tasks, agent-scope chats) can run it; and
      - the CREATOR's own account — the creator holds an active personal
        subscription for it (:func:`subscription_store.list_personal`), so the
        creator's own user-scope chats run it too.

    Never returns a supporting engine. Falls back to the platform default
    engine when no candidate qualifies, so the new agent always has a
    sensible primary engine the creator can finish configuring.

    The agent's ``default_model`` stays empty (Auto → the engine's declared
    default) and ``default_effort`` stays empty (→ High), so the pair
    (engine, model, effort) is fully resolved with zero manual setup.

    Note on the BOTH rule: an admin who *contributes* their only subscription to
    the platform pool but leaves ``use_personal=False`` has no personal row, so
    that engine won't be auto-picked (we fall back to the default). That's an
    accepted edge — the pool engine still works for agent-scope runs, and the
    creator can enable it explicitly afterwards.
    """
    for layer in _auto_enable_candidates():
        platform_connected = bool(subscription_store.list_platform_pool(layer))
        creator_connected = bool(subscription_store.list_personal(layer, user_sub))
        if platform_connected and creator_connected:
            return layer
    return DEFAULT_EXECUTION_PATH
