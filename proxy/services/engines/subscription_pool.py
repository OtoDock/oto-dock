"""Subscription pool manager — selects the right subscription for a session.

Resolution (single chokepoint — see acquire_subscription):
- USER-SCOPE (user_sub set): the user's own accounts (use_personal) first; then,
  only if Platform Auth is on, the admin pool restricted to BORROWABLE API
  credentials (api_key / relay / local_endpoint) — NEVER an admin OAuth
  subscription (those are strictly per-owner; see _USER_BORROWABLE_AUTH_TYPES).
- AGENT-SCOPE (user_sub None): the full platform pool, OAuth subscriptions included.
- None → caller surfaces a "no subscription" block.

Pool selection: scope-sticky first (sessions sharing a credential file stay on
one account — see credential_scope_key), then BYO before relay, then the
two-tier least-consumed headroom sort (5h recent burn, 7d weekly tiebreak),
with the store's least-active order breaking remaining ties.
Bindings are mirrored to the DB (subscription_session_bindings) so usage
attribution and stickiness survive proxy restarts.

Auth credentials reach a session through the ENGINE's adapter
(``ExecutionLayer.subscription_env`` — the pool looks the layer up by the
acquisition's execution path and hands it the ``SubscriptionHandle``):
- API key: the engine's key variable (``ANTHROPIC_API_KEY``, …), or for an
  engine that reads a key from its sign-in file alone (Codex) that file
- OAuth:   the engine's credential FILE payload, which its ``start_session``
  writes into the session's config dir (Claude ``.credentials.json``, Codex
  ``auth.json``). Never an env token: env is frozen at exec, so a live CLI
  could never pick up a rotation, and providers revoke older access tokens
  when the refresh token rotates. The CLIs re-read their credential file
  (Claude: mtime-watch + 401-recovery; Codex: guarded reload), so the pool
  rotates and FANS OUT — see ``ensure_fresh_and_fan_out`` and
  ``services/engines/token_fanout``.
- Local:   the engine's endpoint variable; a minted relay token arrives like
  a BYO key + endpoint

The pool is the SOLE rotator: session files carry a blank refresh token, so a
CLI physically cannot self-rotate (a cascade of CLI-side rotations is exactly
what revoked live sessions' tokens).

All functions are synchronous (call via asyncio.to_thread).

The code lives in eleven pieces beside this module, and this module re-exports every name:
``pool_state``, ``pool_writer``, ``pool_rests``, ``pool_bindings``, ``pool_tokens``, ``pool_select``,
``pool_acquire``, ``pool_limits``, ``pool_rebind``, ``pool_gates`` and ``pool_env``. A test patches the
piece whose code reads a name.
"""

from __future__ import annotations

from core.execution_layer import SubscriptionHandle  # noqa: F401
from services.billing import pool_caps as _pool_caps  # noqa: F401
from services.engines.pool_acquire import (  # noqa: F401
    _SCOPE_REBALANCE_COOLDOWN_S,
    NoSubscriptionError,
    _eligible_candidates,
    _move_scope_group,
    _move_seat,
    _rebalance_scopes,
    _rebind_lock,
    _rebind_tasks,
    _scope_rebalance_last,
    _select_sticky,
    acquire_subscription,
    rebalance_scopes,
    schedule_rebalance,
)
from services.engines.pool_bindings import (  # noqa: F401
    _BOOT_MONOTONIC,
    _LIVENESS_BOOT_GRACE_S,
    _release_rows,
    _sticky_subscription_id,
    bind_session,
    bound_attribution,
    bound_folder,
    bound_oauth_subscription_ids,
    bound_since,
    credential_scope_key,
    drop_ghost_binding,
    get_session_payer_sub,
    get_session_subscription,
    live_session_count,
    moved_within,
    reconcile_active_sessions,
    release_subscription,
    release_unbound_seat,
    restore_session_binding,
    scope_key_for,
    session_bound,
    session_cost_billed,
    session_gone,
    session_registered_live,
    session_token_expiry_ms,
    sessions_on,
    within_boot_grace,
)
from services.engines.pool_env import (  # noqa: F401
    relay_llm_credentials,
    resolve_subscription_env,
)
from services.engines.pool_gates import (  # noqa: F401
    _auto_enable_candidates,
    borrowable_pool_available,
    cap_continue_available,
    default_execution_layer_for_creator,
    layer_platform_configured,
    user_can_run,
    user_scope_block_reason,
)
from services.engines.pool_limits import (  # noqa: F401
    _iso_epoch,
    _rest_until_reset,
    _window_length_s,
    account_freed,
    after_limit,
    follow_ending,
    limit_continues,
    mark_subscription_throttled,
    nudge_from_cli_error,
    rest_after_limit,
    rest_ending_for,
    resume_refused,
    stop_continuation,
    terminal_limit,
)
from services.engines.pool_rebind import (  # noqa: F401
    _rebind_delisted_sessions,
    _selection_contains,
    rebind_delisted_sessions,
    schedule_rebind,
)
from services.engines.pool_rests import (  # noqa: F401
    _LIMIT_ERROR_MARKERS,
    _OVERLOAD_COOLDOWN_S,
    _OVERLOAD_MARKERS,
    _THROTTLE_COOLDOWN_S,
    _end_account_rest,
    _end_scoped_rest,
    _is_throttled,
    _rest_account,
    _rest_end,
    _scope_resting,
    _scoped_rests,
    _throttled_hard,
    _throttled_until,
    _window_rests,
    account_resting,
    clear_rests_with_headroom,
    load_rests,
    looks_like_limit_error,
    throttle_cooldown_for,
)
from services.engines.pool_select import (  # noqa: F401
    _CONSUMPTION_WINDOW_DAYS,
    _CONSUMPTION_WINDOW_HOURS,
    _FAR_FUTURE,
    _cap_status,
    _consumption_window_starts,
    _epoch_or_inf,
    _out_for,
    _rows_without_login,
    _scope_facts,
    _scope_key,
    _ScopeFacts,
    _select,
    _window_exhausted_for,
    _window_readings,
    display_order,
)
from services.engines.pool_state import (  # noqa: F401
    _ACTIVITY_AUTH_TYPES,
    _LOCAL_AUTH_TYPES,
    _SCOPE_RECENT_TTL_S,
    _USER_BORROWABLE_AUTH_TYPES,
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
from services.engines.pool_tokens import (  # noqa: F401
    _AUTH_STREAK_MIN_ATTEMPTS,
    _AUTH_STREAK_MIN_SPAN_S,
    _HARD_EXPIRY_BUFFER_MS,
    _REFRESH_RATELIMIT_COOLDOWN_S,
    _SPAWN_REFRESH_RUNWAY_MS,
    TURN_MIN_TOKEN_RUNWAY_MS,
    _auth_fail_streaks,
    _auto_expire_subscription,
    _build_handle,
    _carry_platform_keys,
    _fan_out_rotated_token,
    _log_refresh_failure,
    _refresh_backoff,
    _refresh_error_terminal,
    _refresh_lock,
    _refresh_locks,
    _refresh_locks_guard,
    _refresh_oauth_token,
    _resolve_oauth_access_token,
    _sustained_auth_dead,
    clear_refresh_backoff,
    ensure_fresh_and_fan_out,
    fan_out_current_token,
)
from services.engines.pool_writer import (  # noqa: F401
    _BINDING_DRAIN_TIMEOUT_S,
    _binding_writer_lock,
    _guarded,
    _queue_write,
    _reopen_binding_writes,
    _writer,
    close_binding_writes,
    flush_binding_writes,
)
from storage.billing import (
    subscription_status,  # noqa: F401
    subscription_store,  # noqa: F401
)
