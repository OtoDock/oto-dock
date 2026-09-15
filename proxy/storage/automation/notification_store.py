"""PostgreSQL-backed notification store.

All functions are synchronous (called via asyncio.to_thread from async code).
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from storage.pg import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Notification definitions ---


def create_notification(
    title: str,
    body: str,
    severity: str = "info",
    scope: str = "user",
    target: str | None = None,
    source: str = "mcp",
    source_id: str | None = None,
    notification_type: str = "one_time",
    schedule: str | None = None,
    run_at: str | None = None,
    interval_seconds: int | None = None,
    created_by: str | None = None,
    notification_id: str | None = None,
    agent_slug: str | None = None,
    chat_id: str | None = None,
    user_tz: str | None = None,
    community_template: str | None = None,
    community_template_item_slug: str | None = None,
) -> dict:
    """Create a notification definition. Returns the full row as dict.

    ``community_template`` + ``community_template_item_slug`` are populated
    when the row is seeded by the community-agents installer. The
    partial unique indexes on those columns make re-seeding idempotent.
    """
    nid = notification_id or str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO notifications
               (id, title, body, severity, scope, target, source, source_id,
                notification_type, schedule, run_at, interval_seconds,
                created_by, created_at, enabled, agent_slug, chat_id, user_tz,
                community_template, community_template_item_slug)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET
                title=EXCLUDED.title, body=EXCLUDED.body, severity=EXCLUDED.severity,
                scope=EXCLUDED.scope, target=EXCLUDED.target, source=EXCLUDED.source,
                source_id=EXCLUDED.source_id, notification_type=EXCLUDED.notification_type,
                schedule=EXCLUDED.schedule, run_at=EXCLUDED.run_at,
                interval_seconds=EXCLUDED.interval_seconds,
                created_by=EXCLUDED.created_by, enabled=EXCLUDED.enabled,
                agent_slug=EXCLUDED.agent_slug, chat_id=EXCLUDED.chat_id,
                user_tz=EXCLUDED.user_tz,
                community_template=EXCLUDED.community_template,
                community_template_item_slug=EXCLUDED.community_template_item_slug""",
            (nid, title, body, severity, scope, target, source, source_id,
             notification_type, schedule, run_at, interval_seconds,
             created_by, now, agent_slug, chat_id, user_tz,
             community_template, community_template_item_slug),
        )
        row = conn.execute("SELECT * FROM notifications WHERE id=%s", (nid,)).fetchone()
        return dict(row)


def get_notification(notification_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM notifications WHERE id=%s", (notification_id,)
        ).fetchone()
        return dict(row) if row else None


def list_notifications(
    scope: str | None = None,
    target: str | None = None,
    source: str | None = None,
    enabled_only: bool = False,
) -> list[dict]:
    conditions: list[str] = []
    params: list[Any] = []
    if scope:
        conditions.append("scope=%s")
        params.append(scope)
    if target:
        conditions.append("target=%s")
        params.append(target)
    if source:
        conditions.append("source=%s")
        params.append(source)
    if enabled_only:
        conditions.append("enabled=TRUE")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM notifications {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def set_notification_enabled(notification_id: str, enabled: bool) -> bool:
    """Flip the enabled flag. Returns True if the row exists."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notifications SET enabled=%s WHERE id=%s",
            (enabled, notification_id),
        )
        return cur.rowcount > 0


# Columns the edit_notification API and MCP tool may touch. Identity (id,
# scope, target, source, created_by, agent_slug, chat_id) is fixed at
# creation. notification_type may flip between one_time and recurring as
# part of a mode switch.
_EDITABLE_NOTIF_COLUMNS = {
    "title",
    "body",
    "severity",
    "schedule",
    "run_at",
    "interval_seconds",  # recurring every N seconds — mutually exclusive with schedule/run_at
    "notification_type",
    "user_tz",  # IANA timezone snapshot — change forces re-register with new trigger TZ
}


def update_notification(notification_id: str, fields: dict) -> bool:
    """Apply a partial update to a notifications row.

    Only columns in ``_EDITABLE_NOTIF_COLUMNS`` are accepted. Pass ``None``
    for a column to set it to NULL (used to clear the opposing timing field
    on mode switch).

    Returns True if the row exists and was updated.
    """
    safe = {k: v for k, v in fields.items() if k in _EDITABLE_NOTIF_COLUMNS}
    if not safe:
        return False
    set_clause = ", ".join(f"{k}=%s" for k in safe.keys())
    params = list(safe.values()) + [notification_id]
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE notifications SET {set_clause} WHERE id=%s",
            params,
        )
        return cur.rowcount > 0


def delete_notification(notification_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM notifications WHERE id=%s", (notification_id,)
        )
        return cur.rowcount > 0


def update_notification_fired(notification_id: str) -> None:
    """Increment fired_count and set last_fired_at."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """UPDATE notifications
               SET fired_count = fired_count + 1, last_fired_at = %s
               WHERE id = %s""",
            (now, notification_id),
        )


# --- Delivery records (the inbox) ---


def create_delivery(
    user_sub: str,
    title: str,
    body: str,
    severity: str,
    scope: str,
    source: str,
    notification_id: str | None = None,
    agent_slug: str | None = None,
    chat_id: str | None = None,
) -> dict:
    """Create a delivery record for a specific user. Returns the full row."""
    did = str(uuid.uuid4())
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO notification_deliveries
               (id, notification_id, user_sub, title, body, severity, scope,
                source, delivered_at, agent_slug, chat_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (did, notification_id, user_sub, title, body, severity, scope,
             source, now, agent_slug, chat_id),
        )
        row = conn.execute(
            "SELECT * FROM notification_deliveries WHERE id=%s", (did,)
        ).fetchone()
        return dict(row)


def get_unread_count(user_sub: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM notification_deliveries "
            "WHERE user_sub=%s AND read=FALSE AND dismissed=FALSE",
            (user_sub,),
        ).fetchone()
        return row["cnt"] if row else 0


def list_deliveries(
    user_sub: str,
    limit: int = 50,
    offset: int = 0,
    include_dismissed: bool = False,
) -> list[dict]:
    conditions = ["user_sub=%s"]
    params: list[Any] = [user_sub]
    if not include_dismissed:
        conditions.append("dismissed=FALSE")
    where = f"WHERE {' AND '.join(conditions)}"
    params += [limit, offset]
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM notification_deliveries {where} "
            f"ORDER BY delivered_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def mark_read(delivery_id: str, user_sub: str) -> bool:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notification_deliveries SET read=TRUE, read_at=%s "
            "WHERE id=%s AND user_sub=%s",
            (now, delivery_id, user_sub),
        )
        return cur.rowcount > 0


def mark_all_read(user_sub: str) -> int:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notification_deliveries SET read=TRUE, read_at=%s "
            "WHERE user_sub=%s AND read=FALSE",
            (now, user_sub),
        )
        return cur.rowcount


def dismiss(delivery_id: str, user_sub: str) -> bool:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notification_deliveries SET dismissed=TRUE, dismissed_at=%s "
            "WHERE id=%s AND user_sub=%s",
            (now, delivery_id, user_sub),
        )
        return cur.rowcount > 0


def dismiss_all(user_sub: str) -> int:
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notification_deliveries SET dismissed=TRUE, dismissed_at=%s "
            "WHERE user_sub=%s AND dismissed=FALSE",
            (now, user_sub),
        )
        return cur.rowcount


# --- Push subscriptions ---


def save_push_subscription(
    user_sub: str,
    platform: str,
    subscription_data: str,
) -> dict:
    """Save or update a push subscription. Returns the row."""
    now = _now()
    with get_conn() as conn:
        # Check for existing
        existing = conn.execute(
            "SELECT id FROM push_subscriptions "
            "WHERE user_sub=%s AND subscription_data=%s",
            (user_sub, subscription_data),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE push_subscriptions SET updated_at=%s WHERE id=%s",
                (now, existing["id"]),
            )
            row = conn.execute(
                "SELECT * FROM push_subscriptions WHERE id=%s",
                (existing["id"],),
            ).fetchone()
            return dict(row)
        sid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO push_subscriptions
               (id, user_sub, platform, subscription_data, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (sid, user_sub, platform, subscription_data, now, now),
        )
        row = conn.execute(
            "SELECT * FROM push_subscriptions WHERE id=%s", (sid,)
        ).fetchone()
        return dict(row)


def get_push_subscriptions(user_sub: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_sub=%s ORDER BY created_at",
            (user_sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_push_subscription(subscription_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM push_subscriptions WHERE id=%s", (subscription_id,)
        )
        return cur.rowcount > 0


def delete_push_subscription_by_data(
    subscription_data: str, user_sub: str | None = None,
) -> bool:
    """Delete a push subscription by its endpoint/token data.

    Pass ``user_sub`` for USER-initiated deletes (the unsubscribe endpoint): the
    delete is then scoped to that owner, so one user cannot unsubscribe another
    user's device by submitting its (observable) endpoint data. Omit it ONLY for
    trusted INTERNAL cleanup (push provider reported the endpoint dead — 410/404
    / token_invalid), where the exact subscription_data is already known-dead.
    """
    with get_conn() as conn:
        if user_sub is None:
            cur = conn.execute(
                "DELETE FROM push_subscriptions WHERE subscription_data=%s",
                (subscription_data,),
            )
        else:
            cur = conn.execute(
                "DELETE FROM push_subscriptions "
                "WHERE subscription_data=%s AND user_sub=%s",
                (subscription_data, user_sub),
            )
        return cur.rowcount > 0


def resolve_username_to_sub(username: str) -> str | None:
    """Resolve a human-readable username to user sub ID.

    Returns the sub if found, None otherwise. Also returns the input
    unchanged if it already looks like a sub (long hex string).
    """
    if not username:
        return None
    # If it's already a long hex-like string, assume it's a sub
    if len(username) > 30:
        return username
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sub FROM users WHERE LOWER(name) = LOWER(%s)", (username,)
        ).fetchone()
        return row["sub"] if row else None


def resolve_sub_to_username(sub: str) -> str | None:
    """Resolve a user sub ID to human-readable username."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM users WHERE sub=%s", (sub,)
        ).fetchone()
        return row["name"] if row else None


def get_all_user_subs() -> list[str]:
    """Get all user sub IDs from the users table."""
    with get_conn() as conn:
        rows = conn.execute("SELECT sub FROM users").fetchall()
        return [r["sub"] for r in rows]


def get_admin_user_subs() -> list[str]:
    """Get user sub IDs of platform admins (notification scope 'admin')."""
    with get_conn() as conn:
        rows = conn.execute("SELECT sub FROM users WHERE role='admin'").fetchall()
        return [r["sub"] for r in rows]


def get_agent_user_subs(agent: str) -> list[str]:
    """Get all user sub IDs assigned to a specific agent."""
    with get_conn() as conn:
        # Include admins (they have access to all agents)
        rows = conn.execute(
            """SELECT DISTINCT sub FROM (
                   SELECT sub FROM user_agents WHERE agent=%s
                   UNION
                   SELECT sub FROM users WHERE role='admin'
               ) t""",
            (agent,),
        ).fetchall()
        return [r["sub"] for r in rows]
