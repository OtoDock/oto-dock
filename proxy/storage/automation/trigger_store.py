"""Trigger CRUD against PostgreSQL.

Mirrors notification_store / database (tasks) patterns. Service-layer
validation lives in services/scheduler/trigger_manager.py — this module is pure
storage.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Columns the edit API and MCP tool may touch. Identity (id, scope, agent,
# created_by) is fixed at creation. slug is also locked once set —
# changing it would break configured webhook URLs. Vendor
# linkage (subscription_id) is also immutable once set; event_filter IS
# editable so users can tune which subset of vendor events fires the trigger.
_EDITABLE_TRIGGER_COLUMNS = {
    "name",
    "task_id",
    "notify_enabled",
    "notify_severity",
    "notify_title",
    "notify_body",
    "notify_target_scope",
    "notify_target",
    "debounce_seconds",
    "event_filter",
}


def create_trigger(
    *,
    slug: str,
    name: str,
    scope: str,
    agent: str,
    created_by: str,
    trigger_id: str | None = None,
    task_id: str | None = None,
    notify_enabled: bool = False,
    notify_severity: str = "info",
    notify_title: str | None = None,
    notify_body: str | None = None,
    notify_target_scope: str | None = None,
    notify_target: str | None = None,
    debounce_seconds: int = 0,
    enabled: bool = True,
    subscription_id: str | None = None,
    event_filter: dict | None = None,
    community_template: str | None = None,
    community_template_item_slug: str | None = None,
) -> dict:
    """Insert a new trigger row. Returns the full row as dict.

    Raises psycopg.errors.UniqueViolation if (scope, agent or created_by, slug)
    collides — caller should map to 400. Also raises on the
    ``idx_triggers_tpl_*`` indexes when re-seeding the same template item
    (idempotency guard for the community-agents installer).

    ``subscription_id`` (FK) and ``event_filter`` (JSONB) tie
    a trigger to a vendor webhook subscription. Generic webhook triggers
    leave both at their defaults (None / {}).
    """
    tid = trigger_id or str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO triggers
               (id, slug, name, scope, agent, created_by,
                task_id,
                notify_enabled, notify_severity, notify_title, notify_body,
                notify_target_scope, notify_target,
                debounce_seconds, enabled,
                subscription_id, event_filter,
                created_at, updated_at,
                community_template, community_template_item_slug)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (tid, slug, name, scope, agent, created_by,
             task_id,
             notify_enabled, notify_severity, notify_title, notify_body,
             notify_target_scope, notify_target,
             debounce_seconds, enabled,
             subscription_id, json.dumps(event_filter or {}),
             now, now,
             community_template, community_template_item_slug),
        )
        row = conn.execute("SELECT * FROM triggers WHERE id=%s", (tid,)).fetchone()
        return dict(row)


def get_trigger(trigger_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM triggers WHERE id=%s", (trigger_id,)).fetchone()
        return dict(row) if row else None


def get_trigger_by_slug(*, scope: str, owner: str, slug: str) -> dict | None:
    """Look up a trigger by URL components.

    For scope='agent', owner is the agent name.
    For scope='user', owner is the creator's user_sub (caller must resolve
    username → user_sub before calling this).
    """
    with get_conn() as conn:
        if scope == "agent":
            row = conn.execute(
                "SELECT * FROM triggers WHERE scope='agent' AND agent=%s AND slug=%s",
                (owner, slug),
            ).fetchone()
        elif scope == "user":
            row = conn.execute(
                "SELECT * FROM triggers WHERE scope='user' AND created_by=%s AND slug=%s",
                (owner, slug),
            ).fetchone()
        else:
            return None
        return dict(row) if row else None


def count_triggers_by_agent() -> dict[str, int]:
    """Return {agent_slug: trigger_count} for all agents with at least one trigger.

    GLOBAL count (every scope + every user). Only for admin/API-key callers; the
    user-facing agents grid uses :func:`count_user_visible_triggers_by_agent` so
    the per-agent number matches what that user sees on the Triggers tab."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS cnt FROM triggers WHERE agent IS NOT NULL GROUP BY agent"
        ).fetchall()
        return {r["agent"]: r["cnt"] for r in rows}


def count_user_visible_triggers_by_agent(user_sub: str) -> dict[str, int]:
    """Return {agent_slug: trigger_count} of triggers ``user_sub`` may see:
    agent-scoped (shared) + their OWN user-scoped. Never counts other users'
    user-scoped triggers. Mirrors ``list_triggers_for_user_view``."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS cnt FROM triggers "
            "WHERE agent IS NOT NULL "
            "AND (scope='agent' OR (scope='user' AND created_by=%s)) "
            "GROUP BY agent",
            (user_sub,),
        ).fetchall()
        return {r["agent"]: r["cnt"] for r in rows}


def list_triggers(
    *,
    agent: str | None = None,
    scope: str | None = None,
    created_by: str | None = None,
    enabled_only: bool = False,
    subscription_id: str | None = None,
) -> list[dict]:
    """List trigger rows with optional filters.

    ``subscription_id`` filter lets the dispatcher cheaply pull
    only triggers attached to a specific vendor subscription.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append("agent=%s")
        params.append(agent)
    if scope:
        conditions.append("scope=%s")
        params.append(scope)
    if created_by:
        conditions.append("created_by=%s")
        params.append(created_by)
    if enabled_only:
        conditions.append("enabled=TRUE")
    if subscription_id:
        conditions.append("subscription_id=%s")
        params.append(subscription_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM triggers {where} ORDER BY created_at DESC", params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_triggers_for_user_view(
    *, user_sub: str, agent: str | None = None,
) -> list[dict]:
    """Triggers visible to a specific user: own user-scoped + all agent-scoped.

    Used by the Triggers tab. Caller still post-filters by `can_access_agent`.
    """
    conditions: list[str] = ["(scope='agent' OR (scope='user' AND created_by=%s))"]
    params: list[Any] = [user_sub]
    if agent:
        conditions.append("agent=%s")
        params.append(agent)
    where = f"WHERE {' AND '.join(conditions)}"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM triggers {where} ORDER BY created_at DESC", params,
        ).fetchall()
        return [dict(r) for r in rows]


def update_trigger(trigger_id: str, fields: dict) -> bool:
    """Apply a partial update to a trigger row.

    Only columns in _EDITABLE_TRIGGER_COLUMNS are accepted. Returns True if
    the row exists and was updated. Caller validates business rules
    (cross-scope task linkage, notify target, etc.) before calling.

    ``event_filter`` is JSON-encoded transparently if passed
    as a dict.
    """
    safe = {k: v for k, v in fields.items() if k in _EDITABLE_TRIGGER_COLUMNS}
    if not safe:
        return False
    # JSON-encode the event_filter dict for JSONB storage.
    if "event_filter" in safe and isinstance(safe["event_filter"], dict):
        safe["event_filter"] = json.dumps(safe["event_filter"])
    safe["updated_at"] = _now()
    set_clause = ", ".join(f"{k}=%s" for k in safe.keys())
    params = list(safe.values()) + [trigger_id]
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE triggers SET {set_clause} WHERE id=%s",
            params,
        )
        return cur.rowcount > 0


def set_trigger_enabled(trigger_id: str, enabled: bool) -> bool:
    """Flip the enabled flag. Returns True if the row exists."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE triggers SET enabled=%s, updated_at=%s WHERE id=%s",
            (enabled, _now(), trigger_id),
        )
        return cur.rowcount > 0


def delete_trigger(trigger_id: str) -> bool:
    """Hard delete. Returns True if the row existed."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM triggers WHERE id=%s", (trigger_id,))
        return cur.rowcount > 0


def record_fire(
    trigger_id: str, *, error: str | None = None,
) -> None:
    """Update fire stats — increment fired_count, set last_fired_at, last_error."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """UPDATE triggers SET
                fired_count = fired_count + 1,
                last_fired_at = %s,
                last_error = %s
               WHERE id = %s""",
            (now, error, trigger_id),
        )


def cleanup_user_triggers(user_sub: str) -> int:
    """Delete all user-scoped triggers for a user. Used on user deletion.

    Returns count of deleted rows.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM triggers WHERE scope='user' AND created_by=%s",
            (user_sub,),
        )
        conn.commit()
        return cur.rowcount


def cleanup_agent_triggers(agent: str) -> int:
    """Delete all triggers for an agent (both scopes). Used on agent deletion."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM triggers WHERE agent=%s", (agent,))
        return cur.rowcount
