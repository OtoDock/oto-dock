"""Execution layer subscription storage.

CRUD operations for the execution_layer_subscriptions and
execution_layer_models tables.  Credentials are Fernet-encrypted
using the same key as credential_store.py.

All functions are synchronous (called via asyncio.to_thread).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from storage.credential_store import _encrypt, _decrypt
from storage.pg import get_conn

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reset_active_sessions() -> int:
    """Reset all active_sessions counters to 0 on startup.

    The in-memory session tracking is lost on restart, so the DB counters
    become stale. This should be called once during proxy startup.
    Returns the number of subscriptions that were reset.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE execution_layer_subscriptions
               SET active_sessions = 0, updated_at = %s
               WHERE active_sessions > 0""",
            (_now(),),
        )
        conn.commit()
        count = cur.rowcount
        if count:
            logger.info(f"Startup: reset active_sessions on {count} subscription(s)")
        return count


# ---------------------------------------------------------------------------
# Subscriptions CRUD
# ---------------------------------------------------------------------------

def list_subscriptions(
    layer: str | None = None,
    owner_sub: str | None = None,
    *,
    use_personal: bool | None = None,
    contribute_platform: bool | None = None,
    include_disabled: bool = False,
) -> list[dict]:
    """List subscriptions with optional filters.

    `owner_sub` / `use_personal` / `contribute_platform` replace the old binary
    `owner_type`. For credential acquisition use the higher-level `list_personal`
    / `list_platform_pool` helpers — they apply the active-only + owner-is-admin
    rules that the resolver depends on.
    """
    with get_conn() as conn:
        sql = "SELECT * FROM execution_layer_subscriptions WHERE 1=1"
        params: list = []
        if layer:
            sql += " AND layer = %s"
            params.append(layer)
        if owner_sub is not None:
            sql += " AND owner_sub = %s"
            params.append(owner_sub)
        if use_personal is not None:
            sql += " AND use_personal = %s"
            params.append(use_personal)
        if contribute_platform is not None:
            sql += " AND contribute_platform = %s"
            params.append(contribute_platform)
        if not include_disabled:
            sql += " AND status != 'disabled'"
        sql += " ORDER BY is_primary DESC, active_sessions ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_platform_pool(layer: str | None = None, provider: str | None = None) -> list[dict]:
    """Subscriptions feeding the platform/agent pool.

    A row qualifies iff ``contribute_platform=TRUE``, ``status='active'``, and it
    is owned by a *current admin* (or is owner-less platform infra such as the
    hosted relay, ``owner_sub=''``). The owner-is-admin check is a SQL JOIN so it
    is atomic and applies to EVERY reader — the resolver and the direct-reading
    title/phone helpers alike — meaning a demoted admin's subscriptions stop
    feeding the pool immediately, before any clear-on-demotion cleanup runs.
    """
    with get_conn() as conn:
        sql = (
            "SELECT s.* FROM execution_layer_subscriptions s "
            "LEFT JOIN users u ON s.owner_sub = u.sub "
            "WHERE s.contribute_platform = TRUE AND s.status = 'active' "
            "AND (s.owner_sub = '' OR u.role = 'admin')"
        )
        params: list = []
        if layer:
            sql += " AND s.layer = %s"
            params.append(layer)
        if provider:
            sql += " AND s.provider = %s"
            params.append(provider)
        sql += " ORDER BY s.is_primary DESC, s.active_sessions ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_personal(layer: str | None, owner_sub: str, provider: str | None = None) -> list[dict]:
    """A user's own usable accounts: owner_sub matches, use_personal=TRUE, active.

    Returns [] for a falsy owner_sub so a blank/None sub can never match the
    ``owner_sub=''`` platform-infra rows.
    """
    if not owner_sub:
        return []
    with get_conn() as conn:
        sql = (
            "SELECT * FROM execution_layer_subscriptions "
            "WHERE owner_sub = %s AND use_personal = TRUE AND status = 'active'"
        )
        params: list = [owner_sub]
        if layer:
            sql += " AND layer = %s"
            params.append(layer)
        if provider:
            sql += " AND provider = %s"
            params.append(provider)
        sql += " ORDER BY is_primary DESC, active_sessions ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_admin_managed(layer: str | None = None, *, include_disabled: bool = True) -> list[dict]:
    """Subscriptions an admin manages in the Execution Layers tab: those feeding the
    platform pool (``contribute_platform``) OR owner-less platform infra (the hosted
    relay + migrated shared keys, ``owner_sub=''``). Owner-less infra is shown even
    with ``contribute_platform`` off, so toggling 'Agent pool' on it can't make it
    vanish (it has no user-settings view to fall back to)."""
    with get_conn() as conn:
        sql = ("SELECT * FROM execution_layer_subscriptions "
               "WHERE (contribute_platform = TRUE OR owner_sub = '')")
        params: list = []
        if layer:
            sql += " AND layer = %s"
            params.append(layer)
        if not include_disabled:
            sql += " AND status != 'disabled'"
        sql += " ORDER BY is_primary DESC, active_sessions ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_subscription(sub_id: str) -> dict | None:
    """Get a single subscription by ID."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM execution_layer_subscriptions WHERE id = %s",
            (sub_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def add_subscription(
    layer: str,
    provider: str,
    auth_type: str,
    owner_sub: str = "",
    use_personal: bool = True,
    contribute_platform: bool = False,
    label: str = "",
    credential_data: dict | None = None,
    oauth_email: str = "",
    is_primary: bool = False,
) -> dict:
    """Add a new subscription. Returns the created record.

    Invariant: owner-less rows (``owner_sub=''``) are platform infra (the hosted
    relay, shared keys with no personal owner) and are never personally usable —
    ``use_personal`` is forced FALSE for them.
    """
    if not owner_sub:
        use_personal = False
    sub_id = str(uuid.uuid4())
    now = _now()
    enc = _encrypt(json.dumps(credential_data or {}))

    with get_conn() as conn:
        # If marking as primary, unset existing primaries for the same owner+layer
        if is_primary:
            conn.execute(
                """UPDATE execution_layer_subscriptions
                   SET is_primary = FALSE, updated_at = %s
                   WHERE layer = %s AND owner_sub = %s""",
                (now, layer, owner_sub),
            )
        conn.execute(
            """INSERT INTO execution_layer_subscriptions
               (id, layer, provider, auth_type, owner_sub, use_personal,
                contribute_platform, label, is_primary, credential_data_enc,
                oauth_email, active_sessions, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 'active', %s, %s)""",
            (sub_id, layer, provider, auth_type, owner_sub, use_personal,
             contribute_platform, label, is_primary, enc, oauth_email, now, now),
        )
        conn.commit()
        return get_subscription_unlocked(conn, sub_id)


def update_subscription(
    sub_id: str,
    **fields,
) -> dict | None:
    """Update subscription fields.

    Supports: label, is_primary, status, use_personal, contribute_platform,
    oauth_email (the provider-reported account identity — stamped on
    reconnect so pre-identity rows converge).
    """
    allowed = {
        "label", "is_primary", "status", "use_personal",
        "contribute_platform", "oauth_email",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_subscription(sub_id)

    now = _now()
    with get_conn() as conn:
        # If setting primary, unset others for the same owner+layer
        if updates.get("is_primary"):
            row = conn.execute(
                "SELECT layer, owner_sub FROM execution_layer_subscriptions WHERE id = %s",
                (sub_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE execution_layer_subscriptions
                       SET is_primary = FALSE, updated_at = %s
                       WHERE layer = %s AND owner_sub = %s""",
                    (now, row["layer"], row["owner_sub"]),
                )

        set_clauses = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values())
        values.append(now)
        values.append(sub_id)
        conn.execute(
            f"UPDATE execution_layer_subscriptions SET {set_clauses}, updated_at = %s WHERE id = %s",
            values,
        )
        conn.commit()
        return get_subscription_unlocked(conn, sub_id)


def delete_subscription(sub_id: str) -> bool:
    """Delete a subscription. Returns True if deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM execution_layer_subscriptions WHERE id = %s",
            (sub_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def increment_active_sessions(sub_id: str) -> None:
    """Atomically increment active_sessions counter."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE execution_layer_subscriptions
               SET active_sessions = active_sessions + 1, updated_at = %s
               WHERE id = %s""",
            (_now(), sub_id),
        )
        conn.commit()


def decrement_active_sessions(sub_id: str) -> None:
    """Atomically decrement active_sessions counter (floor 0)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE execution_layer_subscriptions
               SET active_sessions = GREATEST(0, active_sessions - 1), updated_at = %s
               WHERE id = %s""",
            (_now(), sub_id),
        )
        conn.commit()


def get_credential_data(sub_id: str) -> dict:
    """Decrypt and return the credential_data for a subscription."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT credential_data_enc FROM execution_layer_subscriptions WHERE id = %s",
            (sub_id,),
        ).fetchone()
        if not row or not row["credential_data_enc"]:
            return {}
        return json.loads(_decrypt(row["credential_data_enc"]))


def update_credential_data(sub_id: str, credential_data: dict) -> None:
    """Re-encrypt and update the credential_data for a subscription (e.g. after token refresh)."""
    enc = _encrypt(json.dumps(credential_data))
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE execution_layer_subscriptions SET credential_data_enc = %s, updated_at = %s WHERE id = %s",
            (enc, now, sub_id),
        )
        conn.commit()


def get_pool_stats(layer: str) -> dict:
    """Return pool utilization stats for a layer (contribute_platform subs)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT status, active_sessions
               FROM execution_layer_subscriptions
               WHERE layer = %s AND contribute_platform = TRUE""",
            (layer,),
        ).fetchall()
        total = len(rows)
        active_count = sum(1 for r in rows if r["active_sessions"] > 0)
        total_sessions = sum(r["active_sessions"] for r in rows)
        available = sum(1 for r in rows if r["status"] == "active")
        return {
            "total": total,
            "active": active_count,
            "total_sessions": total_sessions,
            "available": available,
        }


def upsert_session_binding(session_id: str, subscription_id: str, *,
                           layer: str = "", user_sub: str | None = None,
                           scope_key: str = "") -> None:
    """Persist one session→subscription binding (mirror of the pool's
    in-memory map — see ``subscription_pool.bind_session``). ``user_sub`` is
    NULL when the spawn path didn't stamp the acquisition context; '' means
    agent-scope (the in-memory ctx map's exact distinction)."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO subscription_session_bindings
               (session_id, subscription_id, layer, user_sub, scope_key, bound_at)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (session_id) DO UPDATE SET
                   subscription_id = EXCLUDED.subscription_id,
                   layer = EXCLUDED.layer,
                   user_sub = EXCLUDED.user_sub,
                   scope_key = EXCLUDED.scope_key,
                   bound_at = EXCLUDED.bound_at""",
            (session_id, subscription_id, layer, user_sub, scope_key, _now()),
        )
        conn.commit()


def update_session_binding_sub(session_id: str, subscription_id: str) -> None:
    """Swap the persisted binding's subscription (a rotation/rebind landed)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscription_session_bindings SET subscription_id=%s WHERE session_id=%s",
            (subscription_id, session_id),
        )
        conn.commit()


def delete_session_binding(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM subscription_session_bindings WHERE session_id=%s",
            (session_id,),
        )
        conn.commit()


def get_session_binding(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscription_session_bindings WHERE session_id=%s",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def get_scope_binding(scope_key: str) -> str | None:
    """The subscription id most recently bound in this credential scope — the
    persisted half of the pool's scope-sticky lookup (post-restart, when the
    in-memory map is empty but a surviving session may still hold the scope's
    credential file). Newest binding wins."""
    if not scope_key:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT subscription_id FROM subscription_session_bindings
               WHERE scope_key=%s ORDER BY bound_at DESC LIMIT 1""",
            (scope_key,),
        ).fetchone()
        return row["subscription_id"] if row else None


def list_scope_bindings(scope_key: str) -> list[dict]:
    """All persisted bindings for a credential scope, newest first. The pool's
    sticky lookup liveness-checks each row against the live session registries
    (deleting ghosts from un-clean kills) instead of trusting the single newest
    row blindly — a crash leftover otherwise pins the scope to one account for
    the full startup-prune TTL (see ``subscription_pool._sticky_subscription_id``)."""
    if not scope_key:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT session_id, subscription_id, bound_at
               FROM subscription_session_bindings
               WHERE scope_key=%s ORDER BY bound_at DESC""",
            (scope_key,),
        ).fetchall()
        return [dict(r) for r in rows]


def prune_stale_session_bindings(days: int = 7) -> int:
    """Delete bindings older than ``days`` — crash leftovers whose release
    never ran. Bounded staleness keeps an orphaned row from pinning a scope's
    sticky selection (or mis-attributing usage) forever. Returns rows deleted;
    called once at proxy startup."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM subscription_session_bindings WHERE bound_at < %s",
            (cutoff,),
        )
        deleted = cur.rowcount or 0
        conn.commit()
        return deleted


def get_subscription_consumption(sub_id: str, since: str) -> float:
    """Total cost_usd attributed to a subscription since an ISO timestamp.

    Powers the pool's least-consumed (headroom) routing. ``usage_records.source_key``
    carries the subscription id at record time (``stream_pump._record_usage``
    headless; ``transcript_tool_events.record_batch_usage`` interactive).
    """
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) AS total
               FROM usage_records
               WHERE source_key = %s AND created_at >= %s""",
            (sub_id, since),
        ).fetchone()
        return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------------------
# Models CRUD
# ---------------------------------------------------------------------------

def list_models(layer: str | None = None) -> list[dict]:
    """List models, optionally filtered by layer."""
    with get_conn() as conn:
        if layer:
            rows = conn.execute(
                "SELECT * FROM execution_layer_models WHERE layer = %s ORDER BY is_builtin DESC, display_name",
                (layer,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM execution_layer_models ORDER BY layer, is_builtin DESC, display_name"
            ).fetchall()
        return [dict(r) for r in rows]


def add_model(
    layer: str,
    model_id: str,
    display_name: str,
    provider: str = "",
    is_builtin: bool = False,
    context_window: int = 0,
    pricing_input: float = 0,
    pricing_output: float = 0,
    pricing_cache_write: float = 0,
    pricing_cache_read: float = 0,
    supports_reasoning: bool = False,
    supports_xhigh: bool = False,
) -> dict:
    """Add a model with optional pricing info. Returns the created record."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO execution_layer_models
               (layer, provider, model_id, display_name, is_builtin, enabled,
                context_window, pricing_input, pricing_output,
                pricing_cache_write, pricing_cache_read, supports_reasoning,
                supports_xhigh, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (layer, provider, model_id, display_name, is_builtin,
             context_window, pricing_input, pricing_output,
             pricing_cache_write, pricing_cache_read,
             supports_reasoning, supports_xhigh, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM execution_layer_models WHERE layer = %s AND model_id = %s",
            (layer, model_id),
        ).fetchone()
        return dict(row) if row else {}


def update_model(
    model_db_id: int,
    *,
    enabled: bool | None = None,
    context_window: int | None = None,
    pricing_input: float | None = None,
    pricing_output: float | None = None,
    pricing_cache_write: float | None = None,
    pricing_cache_read: float | None = None,
    supports_reasoning: bool | None = None,
    supports_xhigh: bool | None = None,
) -> dict | None:
    """Update a model (enable/disable, pricing, context window, reasoning, xhigh)."""
    sets = []
    vals = []
    if enabled is not None:
        sets.append("enabled = %s")
        vals.append(enabled)
    if context_window is not None:
        sets.append("context_window = %s")
        vals.append(context_window)
    if pricing_input is not None:
        sets.append("pricing_input = %s")
        vals.append(pricing_input)
    if pricing_output is not None:
        sets.append("pricing_output = %s")
        vals.append(pricing_output)
    if pricing_cache_write is not None:
        sets.append("pricing_cache_write = %s")
        vals.append(pricing_cache_write)
    if pricing_cache_read is not None:
        sets.append("pricing_cache_read = %s")
        vals.append(pricing_cache_read)
    if supports_reasoning is not None:
        sets.append("supports_reasoning = %s")
        vals.append(supports_reasoning)
    if supports_xhigh is not None:
        sets.append("supports_xhigh = %s")
        vals.append(supports_xhigh)
    if not sets:
        return None
    now = _now()
    sets.append("updated_at = %s")
    vals.append(now)
    vals.append(model_db_id)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE execution_layer_models SET {', '.join(sets)} WHERE id = %s",
            vals,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM execution_layer_models WHERE id = %s",
            (model_db_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_model(model_db_id: int) -> bool:
    """Delete a custom model. Returns True if deleted. Refuses to delete builtins."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM execution_layer_models WHERE id = %s AND is_builtin = FALSE",
            (model_db_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def sync_builtin_models(layer: str, models: list[dict]) -> None:
    """Sync predefined models from LayerCapabilities into DB.

    Adds missing builtins, updates existing builtins to fully match the registry
    (provider, context_window, pricing, reasoning, xhigh — so release price/model
    updates propagate to existing installs), promotes user-added custom entries to
    builtin when the registry adopts them, and removes stale builtins no longer in
    the config. Preserves admin enable/disable state for current builtins; admin
    pricing customization applies to custom (non-builtin) models only.

    Custom→builtin promotion: if a user previously added a model as custom
    (is_builtin=FALSE) and that same model_id now appears in the registry,
    the existing row is flipped to is_builtin=TRUE with the registry's
    display_name/pricing/etc. This is the expected open-source upgrade path —
    users who added a model ahead of us don't end up with a duplicate or a
    stale custom entry after the platform ships the model as built-in.
    """
    now = _now()
    with get_conn() as conn:
        current_ids = set()
        for m in models:
            model_id = m.get("value", "")
            if not model_id:
                continue  # skip "System Default" entry
            current_ids.add(model_id)
            display_name = m.get("label", model_id)
            provider = m.get("provider", "")
            ctx_win = m.get("context_window", 0)
            p_in = m.get("pricing_input", 0)
            p_out = m.get("pricing_output", 0)
            p_cw = m.get("pricing_cache_write", 0)
            p_cr = m.get("pricing_cache_read", 0)
            reasoning = m.get("supports_reasoning", False)
            xhigh = m.get("supports_xhigh", False)
            # Promote any existing custom row with the same (layer, model_id)
            # to builtin and apply the registry's metadata. Runs BEFORE the
            # insert so the UNIQUE(layer, model_id) constraint doesn't block it.
            conn.execute(
                """UPDATE execution_layer_models
                   SET is_builtin = TRUE,
                       display_name = %s,
                       provider = %s,
                       context_window = %s,
                       pricing_input = %s,
                       pricing_output = %s,
                       pricing_cache_write = %s,
                       pricing_cache_read = %s,
                       supports_reasoning = %s,
                       supports_xhigh = %s,
                       updated_at = %s
                   WHERE layer = %s AND model_id = %s AND is_builtin = FALSE""",
                (display_name, provider, ctx_win, p_in, p_out, p_cw, p_cr,
                 reasoning, xhigh, now, layer, model_id),
            )
            # Insert new builtins (with pricing + reasoning + xhigh flags)
            conn.execute(
                """INSERT INTO execution_layer_models
                   (layer, provider, model_id, display_name, is_builtin, enabled,
                    context_window, pricing_input, pricing_output,
                    pricing_cache_write, pricing_cache_read, supports_reasoning,
                    supports_xhigh, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, TRUE, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (layer, provider, model_id, display_name,
                 ctx_win, p_in, p_out, p_cw, p_cr, reasoning, xhigh, now, now),
            )
            # Update existing builtins to fully match the registry — provider,
            # context_window, pricing, reasoning, and xhigh all follow the
            # registry. The platform owns builtin model definitions, so price /
            # context updates shipped in a release propagate to existing installs
            # on the next sync (no DB shadow). `enabled` is NOT touched here, so
            # admin enable/disable choices are preserved. Admins customize pricing
            # via custom (non-builtin) models, not by editing builtin rows.
            conn.execute(
                """UPDATE execution_layer_models
                   SET provider = %s,
                       context_window = %s,
                       pricing_input = %s,
                       pricing_output = %s,
                       pricing_cache_write = %s,
                       pricing_cache_read = %s,
                       supports_reasoning = %s,
                       supports_xhigh = %s,
                       updated_at = %s
                   WHERE layer = %s AND model_id = %s AND is_builtin = TRUE""",
                (provider, ctx_win, p_in, p_out, p_cw, p_cr, reasoning, xhigh, now, layer, model_id),
            )
        # Remove stale builtins no longer in config
        if current_ids:
            # Use ANY(%s) with a list for IN-clause (psycopg3 way)
            conn.execute(
                """DELETE FROM execution_layer_models
                    WHERE layer = %s AND is_builtin = TRUE
                    AND model_id != ALL(%s)""",
                (layer, list(current_ids)),
            )
        conn.commit()


def remap_retired_model(old_id: str, new_id: str) -> int:
    """One-time remap of a retired builtin model id onto its successor.

    Rewrites every persisted pin of ``old_id`` to ``new_id``:
    ``agents.default_model`` (per-agent pin) and ``chats.model`` (per-chat
    override). Task lanes need no third UPDATE — delegate-spawn
    ``override_model`` is in-memory only, never persisted on dynamic_tasks
    (see services/scheduler/scheduler.py), so a restart re-resolves agent
    defaults which this remap already covers. Plain UPDATEs are idempotent:
    after the first boot both match zero rows. ``updated_at`` is deliberately
    NOT bumped (an administrative rewrite must not reorder chat lists).
    Returns the total number of rows remapped.
    """
    with get_conn() as conn:
        remapped: dict[str, int] = {}
        for table, col in (("agents", "default_model"), ("chats", "model")):
            cur = conn.execute(
                f"UPDATE {table} SET {col} = %s WHERE {col} = %s",  # noqa: S608 — table/col are literals above
                (new_id, old_id),
            )
            if cur.rowcount:
                remapped[f"{table}.{col}"] = cur.rowcount
        conn.commit()
    total = sum(remapped.values())
    if total:
        detail = ", ".join(f"{k}={v}" for k, v in remapped.items())
        logger.info(f"Startup: remapped retired model {old_id} -> {new_id} ({detail})")
    return total


def seed_hosted_llm_subscriptions() -> None:
    """Default-on hosted LLM: ensure a relay platform subscription exists for each
    relay-backed provider on the direct-llm layer, ONCE.

    A ``hosted_llm_seeded`` platform-settings flag guards re-seeding, so an admin
    who later disables a provider isn't silently re-enabled on the next restart.
    New installs (and the first upgrade to this version) get hosted LLM on by
    default; it only actually serves traffic on a paid, in-credit install (else the
    dashboard shows the layer as "not configured"). Idempotent + safe on
    community/air-gapped installs (the relay simply refuses at mint time)."""
    from storage import database as db
    if db.get_platform_setting("hosted_llm_seeded"):
        return
    existing = {
        s.get("provider") for s in list_subscriptions(
            layer="direct-llm", contribute_platform=True, include_disabled=True,
        )
        if s.get("auth_type") == "relay"
    }
    for provider in ("anthropic", "openai", "groq"):
        if provider not in existing:
            add_subscription(
                layer="direct-llm", provider=provider, auth_type="relay",
                owner_sub="", use_personal=False, contribute_platform=True,
                label="OtoDock Hosted", credential_data={},
            )
    db.set_platform_setting("hosted_llm_seeded", "1")


# ---------------------------------------------------------------------------
# User platform auth
# ---------------------------------------------------------------------------

def get_user_allow_platform_auth(user_sub: str) -> bool:
    """Check if user is allowed to use platform subscriptions."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT allow_platform_auth FROM users WHERE sub = %s",
            (user_sub,),
        ).fetchone()
        return bool(row["allow_platform_auth"]) if row else True


def set_user_allow_platform_auth(user_sub: str, allowed: bool) -> None:
    """Toggle platform auth for a user."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET allow_platform_auth = %s WHERE sub = %s",
            (allowed, user_sub),
        )
        conn.commit()


def clear_contribute_platform_for_owner(owner_sub: str) -> int:
    """Pull all of an owner's subscriptions out of the shared platform pool.

    Called on admin demotion (a non-admin may not contribute). Returns the number
    of rows changed. The resolver's owner-is-admin JOIN already excludes a
    non-admin's rows in real time; this keeps the stored state consistent.
    """
    if not owner_sub:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE execution_layer_subscriptions
               SET contribute_platform = FALSE, updated_at = %s
               WHERE owner_sub = %s AND contribute_platform = TRUE""",
            (_now(), owner_sub),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: dict) -> dict:
    """Convert a subscription row to a dict, masking credential data."""
    d = dict(row)
    # Never expose raw encrypted credential data in list responses
    has_creds = bool(d.pop("credential_data_enc", None))
    # Add masked indicator
    d["has_credentials"] = has_creds
    return d


def get_subscription_unlocked(conn, sub_id: str) -> dict:
    """Get subscription within an already-open connection."""
    row = conn.execute(
        "SELECT * FROM execution_layer_subscriptions WHERE id = %s",
        (sub_id,),
    ).fetchone()
    return _row_to_dict(row) if row else {}
