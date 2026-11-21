"""The pool's live maps of bound sessions and the auth-type sets the selection reads.

Every map here is mutated in place and never rebound, so the other pieces import it by name.
"""

from __future__ import annotations

import threading

from core.execution_layer import AUTH_LOCAL

# Track subscription_id → session_id for cleanup on session close.
# Guarded by _session_maps_lock: bind/release mutate it on the event loop while
# the rotation fan-out iterates it on a to_thread worker — an unguarded
# concurrent insert/pop during iteration raises "dictionary changed size".
_session_subscriptions: dict[str, str] = {}  # session_id → subscription_id


_session_maps_lock = threading.Lock()


# session_id → (layer, scope user_sub) — the ACQUISITION context this binding
# was resolved with: the exact (layer, user_sub) pair the config builder passed
# to acquire_subscription ("" = agent-scope/platform pool). Lets a selection
# change (scope checkboxes, delete, disable, Platform-Auth revoke) re-evaluate
# each live session against the same candidate lists and re-home it (see
# rebind_delisted_sessions). Only stamped bindings participate — a session
# bound without context is left alone (fail-soft).
_session_binding_ctx: dict[str, tuple[str, str]] = {}


# session_id → credential scope key (see credential_scope_key) — the live half
# of the scope-sticky lookup: a new spawn in a scope that already has a LIVE
# session bound to account X must reuse X (the scope shares ONE credential
# file; two accounts writing it is the last-write-wins / silent-401 hazard).
_session_scope_keys: dict[str, str] = {}

# session_id → the model it started with (``bind_session``): what the pool
# judges a session by when it has no chat row to read the model from.
_session_models: dict[str, str] = {}

# session_id → when a move swapped its binding (monotonic): a usage report
# that arrives right after may describe the old account.
_session_moved_at: dict[str, float] = {}

# session_id → when it was bound to its account, a move included (epoch
# seconds): of two sessions of one folder on two accounts, the later one's
# account is the folder's (``bound_since``).
_session_bound_at: dict[str, float] = {}


# scope_key → (subscription_id, acquired_at): closes the acquire→bind race —
# a second same-scope spawn between the first spawn's acquisition and its
# bind_session would otherwise miss the live map and pick by headroom.
# Consulted while fresh (see _SCOPE_RECENT_TTL_S), superseded by the live map.
_scope_recent: dict[str, tuple[str, float]] = {}


# Longer than the longest start (a Codex start on a local model may spend
# three minutes on its start command alone): the claim is dropped at the bind
# and at every abandoned start, so the clock only bounds a leak.
_SCOPE_RECENT_TTL_S = 600.0


# sub_id → expiresAt (epoch ms) of the access token most recently ISSUED to a
# spawn by resolve_subscription_env — may be earlier than the store's current
# token when the spawn-time refresh fail-softed to an aging stored token.
_issued_token_expiry: dict[str, int] = {}
# (sub_id, the start's scope key) → the same stamp for that one start: a
# session in a home of its own has a key no other start shares, so its
# snapshot is its own token's even when another start of the account
# renewed between its resolve and its bind (``bind_session`` takes it).
_start_token_expiry: dict[tuple[str, str], int] = {}


# session_id → expiresAt (epoch ms) of the token in that session's credential
# FILE: snapshotted from the issued token by bind_session, then advanced by
# rotation fan-out per session whose file write actually landed (see
# session_token_expiry_ms). A session whose fan-out write failed keeps its old
# snapshot, so the turn-start guard retries the refresh for it.
_session_token_expiry: dict[str, int] = {}


# Auth types a NON-owner user may borrow from the admin pool. OAuth consumer
# subscriptions are deliberately excluded (they are strictly per-owner). To
# restore admin-subscription pooling for users, add 'oauth' here — nothing else
# in the resolver changes.
_USER_BORROWABLE_AUTH_TYPES = frozenset({"api_key", "relay", "local_endpoint"})


#: The rows an engine an agent restricts to local models may take
#: (``agents.local_only_engines``): the self-hosted servers' alone.
_LOCAL_AUTH_TYPES = frozenset({AUTH_LOCAL})


# Auth types whose per-turn cost is an ACTIVITY estimate, not money: a
# subscription's flat fee (oauth) or a local model (local_endpoint). The chat
# hides the cost line + gauge for these (usage is still recorded). Kept as a
# hide-list so an unknown future type keeps showing — today's behaviour.
_ACTIVITY_AUTH_TYPES = frozenset({"oauth", "local_endpoint"})
