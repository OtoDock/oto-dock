"""Phone call-log store — per-call outcome rows for the admin dashboard.

All functions are synchronous (called via asyncio.to_thread from async code).
Rows are written once per call by the daemon's teardown report and read by
the route call-log viewer. Retention follows the caller-data window
(``services/infra/external_retention.py``): the ingest passes the cutoff to
``insert_call`` (opportunistic prune; indexed on ``started_at``; ISO-8601 UTC
strings compare lexicographically) and the daily sweep prunes the rest.
"""

import json
from datetime import datetime, timezone

from storage.pg import get_conn

#: The daemon reports these; anything else is coerced to "failed" at the API.
VALID_OUTCOMES = {
    "completed", "pin_failed", "pin_cooldown", "pin_timeout", "hangup",
    "no_answer", "busy", "failed", "error", "rejected_capacity",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_call(data: dict, *, prune_before: str | None = None) -> int:
    """Insert one call row; with ``prune_before`` (ISO cutoff) also drop the
    rows that started before it. Returns the row id."""
    with get_conn() as conn:
        if prune_before:
            conn.execute(
                "DELETE FROM phone_call_log WHERE started_at < %s", (prune_before,))
        tools = data.get("tools_run") or []
        row = conn.execute(
            """INSERT INTO phone_call_log
               (route_id, route_name, phone_server_id, agent, direction,
                from_number, to_number, transport, call_uuid, outcome,
                pin_attempts, started_at, ended_at, duration_s,
                session_id, identity, tools_run, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s::jsonb, %s)
               RETURNING id""",
            (
                data.get("route_id") or None,
                data.get("route_name", ""),
                data.get("phone_server_id"),
                data.get("agent", ""),
                data.get("direction", "inbound"),
                data.get("from_number", ""),
                data.get("to_number", ""),
                data.get("transport", ""),
                data.get("call_uuid", ""),
                data.get("outcome", "failed"),
                int(data.get("pin_attempts") or 0),
                data.get("started_at") or _now(),
                data.get("ended_at") or None,
                data.get("duration_s"),
                data.get("session_id") or "",
                data.get("identity") or "",
                json.dumps([str(t) for t in tools if t]),
                _now(),
            ),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def prune_older_than(cutoff_iso: str) -> int:
    """Delete the rows that started before ``cutoff_iso``; returns the count."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM phone_call_log WHERE started_at < %s", (cutoff_iso,))
        conn.commit()
        return cur.rowcount


def count_older_than(cutoff_iso: str | None) -> int:
    """Rows that started before ``cutoff_iso`` (None = every row)."""
    with get_conn() as conn:
        if cutoff_iso:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM phone_call_log WHERE started_at < %s",
                (cutoff_iso,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM phone_call_log").fetchone()
        return int(row["n"])


def delete_all() -> int:
    """Forget every call-log row; returns the count."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM phone_call_log")
        conn.commit()
        return cur.rowcount


def list_calls(
    route_id: str | None = None, *, offset: int = 0, limit: int = 50,
) -> tuple[list[dict], int]:
    """Newest-first page of call rows (optionally one route's) + total."""
    where, params = "", []
    if route_id:
        where = "WHERE route_id = %s"
        params = [route_id]
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM phone_call_log {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT * FROM phone_call_log {where}
                ORDER BY started_at DESC, id DESC
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], int(total)
